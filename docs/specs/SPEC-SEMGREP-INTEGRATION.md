# SPEC-SEMGREP-INTEGRATION

## 0. Status

LOCKED 2026-10-21 (consultant review folded in).

- Backlog item: none (owner request, 2026-10-21: after the Sonar fixes and the gate check, add Semgrep checks locally and in CI). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `782b410` or later; `./verify` exits 0; `scripts/prepush-fast.sh` exits 0 (needs `gitleaks`, `actionlint`, `zizmor` on PATH). Local semgrep is `1.181.0` at `~/.local/bin/semgrep`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `semgrep --test` over fixtures (rule behaviour), and `scripts/prepush-fast.sh` end to end (wiring).
- Scope (may edit): the NEW directory `.semgrep/` (rules and test fixtures), `scripts/prepush-fast.sh`, `.github/workflows/python-tests.yml` (the `hygiene` job only), `AGENTS.md` and `CONTRIBUTING.md` (the lines that name the prepush tools).
- Does not touch: `docs/specs/`, `./verify`, `pyproject.toml`, `uv.lock` and the dev extra (no semgrep dependency), any source or test file, `sonar-project.properties`, the `test` and `lint` CI jobs, GitHub repository settings.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Bandit (Python AST), Sonar (taint and quality), gitleaks (secrets) and zizmor plus actionlint (workflows) already run. Registry packs (`p/security-audit`, `p/owasp-top-ten`, `p/secrets`, `p/github-actions`) would mostly duplicate them: for example bandit already skips B608 project-wide for 43 f-string SQL sites, and the same sites would be re-flagged by Semgrep's SQL audit rules; silencing that needs a rule exclusion, which the owner's rule forbids without approval. The value Semgrep adds here is CUSTOM rules that encode this repository's own invariants, which no current tool enforces and which the Sonar work of this run showed regress easily. Decisions taken with the consultant: repo-local rules only (offline, deterministic, no registry fetch, `--metrics=off`), fail on any finding, no baseline mode (it would repeat Sonar's new-code-only blind spot), no SARIF or code-scanning upload (needs `security-events: write` and duplicates the Sonar UI), no dependency in `pyproject.toml` or `uv.lock` (semgrep's dependency tree would add pip-audit surface and resolver conflicts against the Python 3.10 floor), no third-party action (the hygiene job already installs tools with `pipx` and pinned versions). Rejected: a `.semgrepignore` (it REPLACES Semgrep's default ignore list; scope with per-invocation targets and `--exclude` instead); registry packs now (revisit a SHA-pinned upstream slice after reading the Semgrep Rules License); adding it to `./verify` (that gate uses `.venv` tools only).

## 2. Decisions

- D1. Rules live in `.semgrep/rules/`, ONE YAML file per invariant (rule id equals the file stem), with fixtures in `.semgrep/tests/<same stem>.py` annotated with `# ruleid: <id>` and `# ok: <id>` comments (the `semgrep --test` convention). Rules carry NO `paths:` key (a `paths:` filter can make `--test` skip fixtures stored elsewhere); each rule's scope is the target list in the script (D4). Every rule has `severity: ERROR`, a `message` that names the invariant and what to do instead, and `languages: [python]`.
- D2. The initial rule set (the consultant's grep found zero matches today), each verified by the developer to have ZERO matches on the current tree (run it; a match is a real violation: stop and report it, do not exclude it):
  1. `sqlite-connect-outside-db-package`: any call `sqlite3.connect(...)` (the module imported as `sqlite3`; also `from sqlite3 import connect` calls). Scope: `src`, `hooks`, `saltmdb_cli.py`, `saltmdb_server.py`, excluding `src/saltmdb/db`. Invariant: connections are opened only by `src/saltmdb/db/` (AGENTS.md "Database safety"). `scripts/` (benchmarks open copies on purpose) is not in scope.
  2. `shell-true-or-os-shell`: `subprocess.$FUNC(..., shell=True, ...)`, `os.system(...)`, `os.popen(...)`. Scope: `src`, `hooks`, `scripts`, `saltmdb_cli.py`, `saltmdb_server.py`. Also `asyncio.create_subprocess_shell(...)`. Invariant: subprocesses use argument lists (CONTRIBUTING).
  3. `unsafe-deserialization-or-eval`: `pickle.load`/`pickle.loads`, `marshal.loads`, `yaml.load(...)` without a safe loader argument, `eval(...)`, `exec(...)`. Same scope as 2. Invariant: no unsafe deserialization or dynamic code on external input.
  4. `network-host-not-loopback-literal`: `socket.create_connection(($HOST, ...), ...)`, `$S.bind(($HOST, ...))` and a server constructor `$CLS(($HOST, $PORT), ...)` with `metavariable-regex` on `$CLS` of `.*Server$`, where `$HOST` is anything but the string literal `"127.0.0.1"` (a `pattern-not` for that literal). Scope: `src/saltmdb/daemon`, `src/saltmdb/viewer` and `hooks`. Today every such call uses the literal (client.py 216/423/587/788, server.py 797/919/932/1012/1083, hooks/saltmdb-stop-trace-capture.py 137). Invariant: the daemon and viewer talk and listen on loopback only; a variable host is a change that needs review.
  5. `viewer-wfile-write-outside-send-helpers`: `$S.wfile.write(...)` inside a `def $F(...)` whose `$F` does not match `^send_` (shape: `patterns` of the call pattern, a `pattern-inside` of `def $F(...): ...` and `metavariable-regex` on `$F` with `^(?!send_)`; partial-identifier metavariables such as `send_$NAME` are not supported). Known blind spot to document in the rule message comment: a write at module level or in a lambda is not inside a `def` and is missed (none exist). Scope: `src/saltmdb/viewer`.
  6. `no-outbound-http-client`: `urllib.request.urlopen(...)`, `http.client.HTTPConnection(...)`, `http.client.HTTPSConnection(...)`, and calls on the `requests` and `httpx` modules (`requests.$F(...)`, `httpx.$F(...)`). Scope: `src`, `hooks`. Invariant: SALTMDB is local-first and makes no outbound HTTP calls from the product (privacy). Invariant: response bytes leave through the helpers that set the security headers.
  If probing shows a rule cannot be written cleanly with Semgrep OSS (for example a pattern that cannot express "not inside"), the developer reports it as `BLOCKED` with the probe, and the rule is dropped or reshaped by amendment; do not weaken a rule to make it pass.
- D3. Fixtures cover PATTERN behaviour only (scope and `--exclude` cannot be expressed in fixtures, because the rules carry no `paths:`): per rule at least two `# ruleid: <id>` cases (including one alias or `from` import form) and two `# ok:` near-miss cases (such as `sqlite3.Row`, a `127.0.0.1` literal, or `self.wfile.write` inside `def send_x`). Placement: the comment sits on its own line DIRECTLY ABOVE the line where the finding starts (the first line of a multi-line call). The fixtures must be VALID, ruff-clean and ruff-formatted Python (`./verify` runs `ruff check .` and `ruff format --check .` over the whole repository and does not exclude `.semgrep/`): every name defined, every import used, each case inside a small function; run `ruff format` BEFORE finalising the annotations. The fixtures are never imported or executed. The test command is `semgrep scan --test --config .semgrep/rules .semgrep/tests` (not the experimental `semgrep test`) and must exit 0 AND report the expected number of rules and cases: the script fails when the run reports zero tests (Semgrep's default ignore list may skip files named `tests`).
- D4. `scripts/prepush-fast.sh`: add `semgrep` to the missing-tool loop (a missing tool is an error, as for the others), and run, after zizmor, a small block that scans with each rule file against its own targets: `semgrep scan --config .semgrep/rules/<file> --metrics=off --disable-version-check --oss-only --error --quiet <targets> [--exclude <path>]` (`--exclude` only with full relative paths such as `src/saltmdb/db`: a bare name matches at any depth), and the rule tests (`semgrep scan --test --config .semgrep/rules .semgrep/tests --metrics=off`, with the non-zero test count check of D3). Each table row carries its own targets AND its `--exclude` list, so the seeded test of section 4 exercises the exact rows. The scope table of D2 is the single source: keep it as a short bash array or case table in the script, with a comment per row. Use `--jobs 2` (the shared host has 4 cores). Exact flags are checked against `semgrep scan --help` of the pinned version; if a flag name differs, use the real one and report it.
- D5. CI: in the `hygiene` job of `python-tests.yml` add `SEMGREP_VERSION: 1.181.0` to its `env` (pinned, with the comment convention already used) and a step `Install semgrep` with `run: pipx install "semgrep==${SEMGREP_VERSION}"` before `Fast hygiene checks`, with `SEMGREP_ENABLE_VERSION_CHECK: "0"` and `SEMGREP_SEND_METRICS: "off"` in the step env (belt and braces next to the flags) and a comment that pipx pins semgrep but not its transitive dependencies (same as zizmor). No action, no secret, no permission change (`contents: read` stays). Update the job's comment to list semgrep. The job passes only if `scripts/prepush-fast.sh` passes. `actionlint` and `zizmor` must stay clean on the edited workflow (they run in the same script).
- D6. Policy text: a short paragraph in `CONTRIBUTING.md` (where `prepush-fast.sh` is described, about lines 143-155) and the `AGENTS.md` pre-push line: the script now also needs `semgrep`; custom rules live in `.semgrep/rules/`; a hit is fixed, never silenced: `# nosemgrep` and rule exclusions need the owner's approval (same rule as `# nosec` and `# NOSONAR`); how to add a rule (rule file, fixture, scope row). No `nosemgrep` enforcement script in this slice.
- D7. No change to `./verify`, to dependencies or to any source file. The scan must not read or write outside the repository and must not need the network (verify with the network blocked, or state that the tool was not exercised that way).

## 3. Changes per file

| File | Change |
|---|---|
| `.semgrep/rules/*.yml` | six rule files (D2) |
| `.semgrep/tests/*.py` | fixtures (D3) |
| `scripts/prepush-fast.sh` | tool check, scope table, scan loop, rule tests (D4) |
| `.github/workflows/python-tests.yml` | `hygiene` job: version env, install step, comment (D5) |
| `CONTRIBUTING.md`, `AGENTS.md` | the paragraph and the pre-push line (D6) |

## 4. Tests

The fixtures (D3) prove the patterns; the SCOPE rows are proven only by script-level seeded violations, in BOTH directions, on a scratch copy: for every rule one seed inside its scope must be caught (the script exits non-zero and names the rule) and, for the rule with an `--exclude` (sqlite), one seed inside the excluded directory must NOT be caught. Mutation check by the tester on a scratch copy: for each rule, break the pattern in one way (drop the alias form, drop the `pattern-not`, drop the `not-inside`) and show `semgrep --test` fails; remove a row from the scope table in the script and show a seeded violation placed in that scope (in the scratch copy) is no longer caught; seed one violation per rule into a scratch copy of the tree and show `scripts/prepush-fast.sh` exits non-zero and names the rule.

## 5. Documentation

D6 only. `docs/architecture.md` needs nothing.

## 6. Verification procedure

`scripts/prepush-fast.sh` exits 0 on the final tree and prints no semgrep findings; `semgrep --test` passes; `./verify` exits 0 (unchanged tool set); `actionlint` and `zizmor` are part of the script. Evidence of run time (seconds) and of one seeded violation per rule in a scratch copy being caught. Local-only: show `semgrep --version` equals the pinned `1.181.0`.

## 7. Out of scope

Registry packs and an upstream rule slice, SARIF and code scanning, a `nosemgrep` allowlist check, a rule for `hooks/` file opens (added with the hook-transcript slices once their helper exists), the Sonar gate definition (hotspots condition, thresholds) and GitHub ruleset or required-check changes (owner UI actions).

## 8. Acceptance

1. `scripts/prepush-fast.sh` exits 0; `./verify` exits 0. 2. `git status` lists only the files of section 3 (the new directory untracked). 3. The evidence of section 6. 4. Mutation sample of section 4 (tester). 5. After the push, the CI `hygiene` job runs semgrep and is green (architect confirms with `gh run view`).

Amendment 1 (2026-10-21, tester report m_2884, decided by the architect): `metavariable-regex` uses `re.match` (start-anchored) semantics in Semgrep 1.181.0, so the literal `Server$` of D2.4 matches nothing, and the first implementation's `^Server` never fired on the real classes (`SALTMDBTCPServer`, `_ThreadingRpcServer`); D2.4 now reads `.*Server$`, and the fixture must contain ruleid cases named like the two real classes (variable host and `0.0.0.0`) and ok cases with the `127.0.0.1` literal. Seeds through `scripts/prepush-fast.sh` must catch both real class names.
