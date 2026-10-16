# SPEC-DEPENDABOT

## 0. Status

LOCKED 2026-10-16.

- Backlog item: none; slice 3 of the CI work named in SPEC-CI-HARDENING. Shared `context_id`: `saltmdb-dependabot-2026-10`.
- Baseline: commit `e43c0cb` on `develop` (pushed; CI run 38917830392 green). `./verify` exits 0 there: 2251 passed, 1 skipped, 177 subtests passed.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`, clean tree with the spec committed first.
- Test seams: static checks on the config file (YAML parse, zizmor, the repo's fast hygiene script); there is no code to unit test.
- Scope (may edit or create): `.github/dependabot.yml` (new), `scripts/prepush-fast.sh` (the last line only), `CONTRIBUTING.md` (section 4, the CI paragraph only).
- Does not touch: `.github/workflows/`, `pyproject.toml`, `uv.lock`, `src/`, `tests/`, `AGENTS.md`, `docs/` (except this spec).
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Nothing proposes dependency updates. The `mcp` CVE was found only because `pip-audit` failed in the gate, and every GitHub Action is pinned to a commit SHA, which never moves by itself. Dependabot version updates keep both fresh with small pull requests that the existing CI jobs check.

Decisions already taken (architect, within the owner's go-ahead for this slice):

- Dependabot reads `dependabot.yml` only from the default branch (`master`; GitHub docs, "about the dependabot.yml file"). A file committed to `develop` does nothing until it reaches `master`, and any change to `master` needs the owner's explicit authorization. This spec therefore ships the file on `develop` only and the updates stay inactive until the owner puts the file on `master` (by a merge of `develop` or a commit of that one file; the owner decides). `target-branch: develop` then makes the pull requests land on `develop`.
- Two ecosystems: `github-actions` (the workflow pins by SHA with a version comment, which Dependabot updates) and `uv` (the repo has `pyproject.toml` and `uv.lock` at the root, and CI uses `uv sync --locked`).
- Version-update pull requests target `develop` (`target-branch`), not the default branch `master`. Security-update pull requests ignore `target-branch` and go to `master`; this spec does not change that.
- `versioning-strategy: lockfile-only` for `uv`: refresh `uv.lock` inside the ranges `pyproject.toml` already allows, never edit those ranges. A fix that needs a range change still arrives the way the `mcp` CVE did, through `pip-audit` failing, and is then done by hand.
- A 7-day cooldown on both ecosystems, so a freshly published release is not proposed at once (supply-chain hygiene). Seven days is also the threshold of zizmor's `dependabot-cooldown` audit: probed at lock time, an update entry without it gives a medium finding, and with it none.
- Weekly on Monday, at most 3 open pull requests per ecosystem, minor and patch updates grouped into one pull request per ecosystem; major updates stay one pull request each.
- Rejected: `pip` (this repo locks with uv); daily schedules and a larger pull request limit (a flood of pull requests for a solo owner); `increase` strategies (they rewrite the ranges in `pyproject.toml`, which is a decision for a person).
- Not decided here, and not done by this spec: who merges Dependabot pull requests, and when. That is the owner's call (section 7).

## 2. Decisions

- D1. `.github/dependabot.yml` has `version: 2` and two `updates` entries, exactly as in section 3.1.
- D2. `scripts/prepush-fast.sh` audits `.github` instead of `.github/workflows`, so the same zizmor run covers `dependabot.yml` locally and in the CI `hygiene` job (which runs this script). The comment line above it keeps its meaning.
- D3. `CONTRIBUTING.md` section 4, CI paragraph (lines about the `lint`, `test` and `hygiene` jobs): one sentence saying `.github/dependabot.yml` configures weekly Dependabot updates for GitHub Actions and for the locked Python dependencies, to `develop`, that Dependabot reads the file only from the default branch (so it is inactive until the file is on `master`), and that each update goes through the same CI jobs. It must not say updates are already running, that they are merged automatically, or that anyone is required to merge them.

## 3. Changes per file

### 3.1 `.github/dependabot.yml` (new)

```yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    target-branch: develop
    schedule:
      interval: weekly
      day: monday
    cooldown:
      default-days: 7
    open-pull-requests-limit: 3
    commit-message:
      prefix: "ci(deps)"
    groups:
      actions:
        patterns:
          - "*"
        update-types:
          - minor
          - patch

  - package-ecosystem: uv
    directory: /
    target-branch: develop
    versioning-strategy: lockfile-only
    schedule:
      interval: weekly
      day: monday
    cooldown:
      default-days: 7
    open-pull-requests-limit: 3
    commit-message:
      prefix: "build(deps)"
    groups:
      python-minor-patch:
        patterns:
          - "*"
        update-types:
          - minor
          - patch
```

Add a one-line comment above each `target-branch`, `versioning-strategy` and `cooldown` line saying why (taken from section 1). The probe of this exact file (without comments) parsed with `yaml.safe_load` and passed zizmor with no findings.

### 3.2 `scripts/prepush-fast.sh`

The last line, `zizmor --no-progress --no-online-audits .github/workflows`, becomes `zizmor --no-progress --no-online-audits .github`. Change nothing else.

### 3.3 `CONTRIBUTING.md`

D3, one sentence in the CI paragraph, kept to the existing wrap width.

## 4. Tests

None; there is no code. The static checks in section 8 are the check, and the first Dependabot run after the push is the real one (section 8, post-implementation).

## 5. Documentation

D3 only.

## 6. Verification procedure

None beyond section 8.

## 7. Out of scope

- Merging, approving or closing any Dependabot pull request; deciding a merge policy; auto-merge; labels; reviewers.
- Security updates (a repository setting; their pull requests go to `master`, which only the owner may act on).
- Changing any version range in `pyproject.toml` or any pinned action by hand.
- The Sonar gate enforcement (next slice).
- Putting `dependabot.yml` on `master`, or merging `develop` into `master` (owner's explicit authorization).
- Dependabot cannot update the `version:` input of setup-uv or the `*_VERSION` env pins in the `hygiene` job; they stay manual.

## 8. Acceptance

Run from the repo root with the repo `.venv`.

1. `git status --short` shows nothing before you start.
2. `.venv/bin/python -c "import yaml; d = yaml.safe_load(open('.github/dependabot.yml')); print([u['package-ecosystem'] for u in d['updates']])"` prints `['github-actions', 'uv']`; and in the same parsed data both entries have `target-branch == 'develop'` and `cooldown['default-days'] == 7`, and the `uv` entry has `versioning-strategy == 'lockfile-only'` (assert these in the same `yaml.safe_load` one-liner or a small inline script, not with `rg`, because the required comments repeat the words). The `github-actions` group `actions` has `update-types == ['minor', 'patch']`.
3. `zizmor --no-progress --no-online-audits .github` exits 0 and prints no findings. Negative control, in a scratch copy only (never in the repo): copy `.github/` into an empty scratch directory, delete the two `cooldown` blocks from the copy, and run the same command from that directory; it reports two `dependabot-cooldown` findings and exits non-zero (13), which is the expected result there, not a failed step. Report both outputs and delete the scratch directory.
4. `scripts/prepush-fast.sh` exits 0, and its output shows zizmor completing `.github/dependabot.yml` as well as `.github/workflows/python-tests.yml`.
5. `git diff scripts/prepush-fast.sh` shows exactly the one changed line.
6. `./verify` exits 0 (run last).
7. `git diff --stat` plus `git status --short` list exactly the three scope files, the new one untracked.

Post-implementation, architect-owned: push to `develop` and tell the owner that Dependabot stays inactive until the file is on `master`. After the owner has put it there: confirm GitHub accepted the config (`Dependabot Updates` runs appear under `gh run list` and do not fail with a config error; read the first run's log), check the first `uv` pull request's `uv.lock` diff for unwanted header or revision changes (CI pins uv 0.11.7, the local uv is newer), list the pull requests (`gh pr list --author app/dependabot`) and tell the owner; do not merge any.

## 9. Pre-lock gate notes

| Claim | Where | Verified how |
| --- | --- | --- |
| `uv` is a supported Dependabot ecosystem, with `cooldown`, `groups`, `versioning-strategy` (all five values) and `commit-message.prefix`; `target-branch` applies to version updates only and security updates use the default branch | GitHub docs, Dependabot options reference | read (WebFetch, two page ranges) |
| `cooldown.default-days` is supported for both `uv` and `github-actions` | same page, cooldown table | read |
| zizmor 1.30.1 audits `dependabot.yml` when given `.github`, and reports `dependabot-cooldown` (medium) when `cooldown` is missing | scratch copy of the file | probed: 0 findings with cooldown, 2 findings without |
| `prepush-fast.sh` audits only `.github/workflows` today | `scripts/prepush-fast.sh`, last line | read |
| `.github/` holds only `workflows/` | `ls .github` | probed |
| CI `pull_request` triggers include `develop`, and the scan step skips Dependabot (`pull_request.user.login`) | `.github/workflows/python-tests.yml` | read |
| CI uses `uv sync --locked`, so a lock-only Dependabot change is checked as-is | workflow | read |
| The workflow pins actions by SHA with a version comment | workflow | read; Dependabot's handling of SHA pins is documented behavior, not probed: the first run is the test |
| No existing Dependabot config, and no mention of Dependabot in CONTRIBUTING or AGENTS | `rg -in dependabot` | probed (only a backlog row about past alerts) |
| Dependabot reads `dependabot.yml` only from the default branch (`master`) | GitHub docs, about the dependabot.yml file (consultant) | read by the consultant, not re-fetched by the architect |
| Without `update-types` a group includes major updates too; a `commit-message.prefix` ending with `)` gets a colon added; Dependabot's github-actions updater moves the SHA and the same-line version comment | GitHub docs (consultant) | read by the consultant |
| `groups.<name>.patterns` is allowed together with `update-types`; a `uv` group without `patterns` might be rejected | docs example | `patterns: ["*"]` added to the group for safety; not probed against GitHub |

Checks run: scope list against every acceptance command (steps 2 to 7 touch only the three scope files). No string or constant is changed (gate step 5 does not apply). The values stated twice (`target-branch: develop`, `default-days: 7`, `lockfile-only` in section 1 and section 3.1 and acceptance step 2) were compared word for word. Scenario walk: a Dependabot pull request for an action changes one workflow line and runs `test`, `lint`, `hygiene`, with the Sonar step skipped; a `uv` pull request changes `uv.lock` and runs the same jobs; a major update is not grouped. New-code acceptance (steps 2 to 7) needs the implementation and runs afterward. Baseline gate not re-run by the architect: the pushed commit's CI stands as the baseline.
