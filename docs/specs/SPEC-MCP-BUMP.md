# SPEC-MCP-BUMP

## 0. Status

LOCKED 2026-10-16.

- Backlog item: none; follow-up named in SPEC-CI-HARDENING Amendment 2. Shared `context_id`: `saltmdb-mcp-bump-2026-10`.
- Baseline: commit `ee72917` on `develop`. The full gate cannot be green on it because `pip-audit` reports `mcp` 1.29.0, `CVE-2026-59951` (fixed in 1.30.0 and 2.2.0). The other gates were reported green by the CI-hardening developer on this tree: 2251 passed, 1 skipped, 177 subtests passed. The architect did not re-run them at lock time.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`, clean tree with the spec committed first.
- Test seams: the existing suite through `./verify`. No new tests.
- Scope (may edit): `pyproject.toml` (line 16 only) and `uv.lock`.
- Does not touch: `src/`, `tests/`, `hooks/`, `docs/`, `.github/`, `scripts/`, any other dependency.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`./verify` exits 1 at `pip-audit` on a single finding, `mcp` 1.29.0 (`CVE-2026-59951`, fixed in 1.30.0 and 2.2.0). The lint job in CI runs the same gate, so `ee72917` must not be pushed until it is green. The owner approved the bump on 2026-10-16.

The range `>=1.28.1,<2.0.0` already admits 1.30.0, so the minimum change is a lock update. Decision taken here: also raise the floor to 1.30.0, so a fresh resolve can never pick a vulnerable 1.28.x or 1.29.x. Rejected: raising to 2.2.0 (a major version, outside the `<2.0.0` cap, a separate decision); leaving the floor (the manifest would keep admitting a version `pip-audit` rejects).

## 2. Decisions

- D1. `pyproject.toml` line 16 becomes `"mcp>=1.30.0,<2.0.0",`.
- D2. `uv.lock` is regenerated with `uv lock --upgrade-package mcp`. A dry run at lock time reported `Update mcp v1.29.0 -> v1.30.0` and no other change.
- D3. The development environment is synced to the lock with `uv sync --extra dev`, and the suite runs on 1.30.0.
- D4. A test failure caused by 1.30.0 is not fixed here. Stop and report `BLOCKED — SPEC ADJUDICATION REQUIRED` with the failing test and traceback. The surfaces coupled to the `mcp` SDK, all checked against the 1.30.0 source diff at lock time and expected unchanged: `src/saltmdb/mcp/tools.py:1839-1842` (private `mcp._tool_manager.list_tools()` and `fn_metadata.arg_model`; its `# shortcut:` comment names an `mcp` version change as the trigger, and this bump is that trigger); tests that reach into `tools.mcp._tool_manager._tools` (`tests/test_phase4_mcp_surface.py`, `test_phase3_mcp_surface.py`, `test_mcp_tools.py`, `test_omp_harness.py`, `test_retrieve_context_wiring.py`, `test_corpus_snapshot_mcp.py`); tests importing `mcp.server.fastmcp.exceptions.ToolError` (`tests/test_mcp_tools.py`); and `tests/test_mcp_tools.py:1893-1897`, which asserts `outputSchema` is None for `search_memory`.
- D5. The target is `mcp` 1.30.0. If a later 1.x release exists when the work starts, `uv lock --upgrade-package mcp` picks it; that is acceptable, report the version it picked and keep the floor at `>=1.30.0`.

## 3. Changes per file

### 3.1 `pyproject.toml`

Line 16: replace `"mcp>=1.28.1,<2.0.0",` with `"mcp>=1.30.0,<2.0.0",`. Change nothing else.

### 3.2 `uv.lock`

Run `uv lock --upgrade-package mcp`. Expected diff: the `[[package]] name = "mcp"` entry (version, sdist and wheel hashes, and any dependency change 1.30.0 makes) and the specifier on the `saltmdb` package's `requires-dist` line for `mcp`. No other package entry changes. If another package entry changes, stop and report it.

## 4. Tests

None added. The existing suite is the check (section 8).

## 5. Documentation

None.

## 6. Verification procedure

Do not start a parallel full suite; other processes may use the host (AGENTS.md). Run the gates in section 8 in order.

## 7. Out of scope

- Moving to `mcp` 2.x.
- Fixing any other dependency, advisory or Sonar finding.
- Editing the earlier specs that name 1.29.0 (they record history).

## 8. Acceptance

Run from the repo root with the repo `.venv`.

1. `git status --short` shows nothing before you start.
2. Apply D1 (edit `pyproject.toml` line 16) before locking; the lock records the specifier.
3. `uv lock --upgrade-package mcp` prints `Update mcp v1.29.0 -> v1.30.0` (or a later 1.x, D5).
4. `uv sync --extra dev` exits 0, and `.venv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"` prints `1.30.0` (or the version from step 3).
5. `uv lock --check` exits 0.
6. `./verify` exits 0 (all gates, including `pip-audit --skip-editable`, which needs network access; the output should say no known vulnerabilities).
7. `git diff --stat` lists exactly `pyproject.toml` and `uv.lock`, and `git diff pyproject.toml` changes only line 16.
8. `git diff -U0 uv.lock` touches only the `mcp` package entry (version, sdist, wheels; the dependency list is identical in 1.29.0 and 1.30.0) and the `mcp` specifier line (`uv.lock:2471`); the bare `{ name = "mcp" }` line at 2443 does not change. Report anything else.

If `./verify` fails on a test, rerun that test once alone to rule out a flake, then apply D4.

## 9. Pre-lock gate notes

Claims table.

| Claim | Where | Verified how |
| --- | --- | --- |
| `pyproject.toml:16` is `"mcp>=1.28.1,<2.0.0",` | `pyproject.toml` | read |
| One `mcp` package entry (`uv.lock:859`), one specifier line (`uv.lock:2471`), one bare dependency line (`uv.lock:2443`) | `uv.lock` | `rg -n 'name = "mcp"'` |
| Lock update changes only `mcp` | `uv lock --upgrade-package mcp --dry-run` printed one line, `Update mcp v1.29.0 -> v1.30.0` | probed |
| `./verify` runs `pip-audit --skip-editable` | `verify:24` | `rg` |
| 1.30.0 leaves the stdio, tool manager, exceptions and types modules unchanged | consultant `diff -rq` of the 1.29.0 and 1.30.0 wheels | consultant probe, not re-run by the architect |
| `tools.py:1839-1842` uses `_tool_manager.list_tools()` | `src/saltmdb/mcp/tools.py` | read |
| `tests/test_mcp_tools.py:1893-1897` asserts `outputSchema` is None | `tests/test_mcp_tools.py` | read |
| No code uses an initialize stub, `StdioServerParameters` or `stdio_client` | `src`, `tests`, `hooks` | `rg -c` printed no hits |
| No doc names an `mcp` version pin that this changes | INSTALL, MIGRATION, README, CONTRIBUTING | consultant grep |

Checks run: scope list (`pyproject.toml`, `uv.lock`) against acceptance step 8 and the dry run, agreeing. The lockfile is named in scope (gate step 4). Rule changes after review (step order, D4, D5) were re-grepped across the spec for the old wording. Step 6 and the `pip-audit` result need the new code and the network, so they run after implementation. Consultant review: one pass, findings applied. Baseline gate not re-run by the architect; the pre-existing failure is the `pip-audit` finding this spec removes.
