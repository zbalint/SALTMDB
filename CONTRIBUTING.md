# Contributing to SALTMDB

We welcome contributions from the open-source community! Follow these steps to set up your environment, write tests, and submit your pull requests.

---

## 1. Local Development Setup

1. **Fork the Repository:** Create your own fork of this repository on your version control hosting provider.
2. **Clone locally:**
   ```bash
   git clone https://github.com/your-username/SALTMDB.git
   cd SALTMDB
   ```
3. **Install dependencies (recommended: `uv`):** this repo ships a committed `uv.lock` for
   deterministic, reproducible dependency resolution.
   ```bash
   # Install uv once: https://docs.astral.sh/uv/getting-started/installation/
   uv sync --extra dev   # creates .venv and installs runtime + dev tooling deps
   uv run python -m unittest discover -s tests   # or: uv run <any command>
   ```
   A manual `venv`/`pip` setup still works if you'd rather not use `uv`:
   ```bash
   python -m venv .venv
   # On Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # On Unix:
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

---

## 2. Writing Code & Guidelines

* **Preserve Docstrings:** Maintain code documentation, type hints, and comment structures where possible.
* **Database Safety:** Ensure that all changes to database schemas or routines do not disrupt sqlite3 concurrency features (WAL mode, transactions, timeout structures).
* **Minimal External Dependencies:** Only add third-party packages when strictly necessary and when they ship prebuilt wheels for all supported platforms (Windows, Linux, macOS). New dependencies must be justified in the PR description.

### 2.1 Behavioral Coding Rules (AI-Agent & Human Contributors)

These are discipline-only rules — no new tooling required to follow them (see §4 for the tool-backed gates
that check some of these mechanically).

**Reuse over reinvention**
1. Before writing any new helper/utility, search existing modules first and confirm nothing
   reusable already exists.
2. Prefer stdlib (`pathlib`, `functools`, `itertools`, `typing`, etc.) over a new third-party
   dependency.
3. When a small capability gap is found in an existing function, extend its signature (updating
   callers) rather than adding a second near-identical helper.

**Surgical, minimal-diff edits**
4. Targeted search-and-replace edits, not full-file rewrites; no full-file regeneration above
   ~30 lines unless explicitly requested.
5. Never touch unmodified functions, reorder imports, or refactor neighboring logic as a side
   effect of an unrelated fix.
6. State target files/line ranges before editing; if the true fix spills outside that boundary,
   stop and re-scope rather than expanding silently.
7. Check `git diff` before calling anything done, to catch stray/collateral changes.

**Secure coding**
8. Never use `eval()`, `exec()`, or `pickle.loads()` on untrusted/external input.
9. Run subprocesses as `subprocess.run([...], shell=False)` with argument lists, never
   shell-interpolated strings; always use parameterized queries for DB access.
10. Don't request or use broader filesystem/shell/API access than the task actually needs.

**Performance discipline**
11. Never refactor "for performance" without measured before/after data (`cProfile`/`timeit`);
    no speculative micro-optimizations.
12. When a real bottleneck is confirmed, prefer an algorithmic/data-structure fix (e.g.
    list-scan → `set`/`dict`) over local micro-tweaks.

**Avoiding AI slop**
13. Don't create single-use helper abstractions for something used exactly once inline.
14. Don't swallow exceptions silently — no bare `except: pass`.
15. Don't copy-paste logic that already exists elsewhere — import/reuse it.

**Self-verification**
16. Run `PYTHONPATH=src python -m unittest discover -s tests` as a mandatory last step before
    declaring any code change done — inspect real failures, fix, re-run.
17. For new features, prefer writing/extending a failing test first, then implement to green.

---

## 3. Testing Changes

Every modification must pass the unit test suite before submission.

1. Set the PYTHONPATH environment variable:
   ```bash
   # On Windows:
   $env:PYTHONPATH="src"
   # On Unix:
   export PYTHONPATH="src"
   ```
2. Run the unit test suite (matches the command CI enforces in `.github/workflows/python-tests.yml`):
   ```bash
   PYTHONPATH=src python -m pytest tests/
   ```
3. Start or use the SALTMDB daemon with the viewer enabled first (it is enabled by default; set
   `SALTMDB_VIEWER_ENABLED=true` if needed). Then check the viewer status by running:
   ```bash
   python -m saltmdb.viewer.server
   ```
   This is a status check, not a viewer launcher; it reports the daemon's viewer URL to open in a
   browser.

---

## 4. Tooling & Quality Gates

Adopted 2026-07-31 on top of the behavioral rules in §2.1, closing the tooling gap (no
lint/type/security config existed in this repo before then).
Config lives in `pyproject.toml` under `[tool.ruff]`/`[tool.mypy]`/`[tool.bandit]`/`[tool.deptry]`.

* **`ruff check` / `ruff format`** — lint (`E`, `F`, `W`, `C90` complexity, `PLR` refactor
  metrics) and formatting. `E501` (line-too-long) is disabled: the formatter already wraps
  executable code to `line-length = 100`; remaining long lines are string/comment literals it
  won't split. A handful of pre-existing complexity findings (`C901`, `PLR0912`, `PLR0915`,
  `PLR0911`) and two intentional `sys.path`-before-import cases (`E402`) are marked with
  `# noqa` as a one-time adoption baseline (2026-07-31) — new code should not add more of these
  without a specific reason.
* **`mypy`** — incremental/basic-mode type checking. Public MCP tool signatures in `mcp/tools.py`
  use the explicit `X | None = None` form throughout.
* **`bandit`** — security scan. `B608` (hardcoded-SQL-expression) is disabled at the project
  level: verified (2026-07-31) as a systemic false positive across every dynamic-SQL call site —
  all f-string interpolation is either a `",".join("?" for _ in ids)` placeholder run or an
  internal config constant, never attacker-controlled text; actual values are always bound via
  `?` parameters. Everything else pre-existing (mostly `B110`/`B112` bare `except: pass`/
  `continue`, and `subprocess`/`urlopen` findings in the viewer) remain reported by Bandit under
  the project-level configuration in `pyproject.toml`. **The pre-existing findings are a real
  follow-up item, not a closed matter** — in particular the 17 `try/except: pass` sites directly
  overlap rule #14 in §2.1 and deserve a dedicated review pass, not a rewrite rushed through
  alongside tool adoption.
* **`pip-audit`** — dependency CVE / typosquat scanning against `pyproject.toml`'s dependencies.
* **`deptry`** — unused/missing/transitive dependency detection. `numpy` was added as an
  explicit direct dependency (2026-07-31) after deptry caught it being imported directly
  (`memory_service.py`, cosine similarity) while only present transitively via `fastembed`.

Run everything locally before pushing:
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run bandit -c pyproject.toml -r src -q
uv run pip-audit --skip-editable
uv run deptry src
```
Or install the git hook once via `uv run pre-commit install` to run the same checks
automatically on every commit (`.pre-commit-config.yaml`; the `pip-audit`/`deptry` steps are
CI-only — network/full-env cost makes them a poor fit for every local commit). CI enforces the
same set as a required `lint` job (`.github/workflows/python-tests.yml`) alongside the existing
`test` job.

**Known follow-up items** (found during 2026-07-31 tool adoption, deliberately not fixed as part
of it — each needs its own reviewed change):
* 17 `try/except: pass` + 2 `try/except: continue` sites (`bandit` B110/B112) — audit
  against §2.1 rule #14.
* 50 pre-existing `ruff` complexity findings (`C901`/`PLR0912`/`PLR0915`/`PLR0911`), noqa'd as a
  baseline — candidates for a dedicated refactor pass, not a drive-by fix.

---

## 5. Documentation Sync Checklist (Version Bumps & Signature Changes)

Docs and skill files here don't auto-sync with the code or with each other — a 2026-07-25 audit found `MIGRATION.md` and `MULTI_AGENT_ORCHESTRATION.md` had both drifted silently for multiple releases before anyone noticed. If your change touches any of the following, check the matching box before opening a PR:

* [ ] **Bumped `pyproject.toml` version, OR changed the `entities`/`events` schema, OR changed a public MCP tool's parameters** → add a row to `MIGRATION.md`'s Version Schema Registry describing the change and required migration action (or explicitly note "No Action Required").
* [ ] **Changed a public MCP tool's name, parameters, or return shape** (anything in `src/saltmdb/mcp/tools.py`) → update the matching entry in `AGENT_GUIDE.md` §2 (Available Tools Overview) so the documented signature still matches the code.

> [!NOTE]
> **Repo-root orchestration templates retired (`v0.1.0-alpha.74`).** `ORCHESTRATOR.md`, `MULTI_AGENT_ORCHESTRATION.md`, and `WORKER_TEMPLATE.md` used to exist as a vendor-neutral reference copy of the multi-agent orchestration protocol at the repo root, alongside a Claude-Code-specific implementation bundled at `~/.claude/skills/saltmdb-subagent-orchestration/references/`. Maintaining two independently-drifting copies (see the 2026-07-25 audit referenced above, and `MIGRATION.md` alpha.49–.51) turned out not to be worth it, so the repo-root copies were deleted; the bundled skill copy is now the sole reference implementation. `AGENT_GUIDE.md` §1 commandment 14 and §6 step 3 were updated accordingly — if you change the orchestration protocol, only the skill bundle needs updating, and there's no longer a "keep the repo root vendor-neutral" constraint to reconcile against.

---

## 6. Submitting Pull Requests

1. **Create a Feature Branch:** Choose a descriptive name (e.g. `feature/add-log-rotation`).
2. **Commit clearly:** Follow standard commit message guidelines detailing *why* the change was made.
3. **Open a PR:** Describe your implementation decisions and link any related issues.
