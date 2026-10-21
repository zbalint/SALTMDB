# SPEC-PYTHON-FLOOR-311

## 0. Status

LOCKED 2026-10-21 (consultant pre-lock review m_2918 folded in).

- Backlog item: none. Sonar gate on `develop` `a039975` is ERROR: condition `new_sca_severity_any_issue` is 10 against a threshold of 9. Sonar SCA lists 10 records: eight OPEN `pillow 12.3.0` items (CVE-2026-112395, -112397, -112400, -112403, -112404, -112895, -112896, -112897) plus two `fsspec` CVE-2026-104851 records (SAFE and FIXED, no action). The pillow items come from the `uv.lock` resolution marker for Python < 3.11, kept only because `requires-python` is `>=3.10` and `fastembed` (0.9.0, latest) declares `pillow<13`. Pillow 13.0.0 fixes them and needs Python 3.11. Owner decision (2026-10-21): "we can go up to 3.12.5 python version", i.e. the Python floor may rise; no Accept/Safe, no suppression. This spec raises the floor to 3.11 (the minimum that removes the vulnerable resolution; CI and the dev venv already run 3.11). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `a039975` or later (Sonar gate ERROR on SCA; `./verify` exits 0, 3221 passed); `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, `tests/test_daemon_server.py::TestDaemonSignalShutdown`; rerun a failing one alone once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Scope (may edit): `pyproject.toml`, `uv.lock` (regenerated), `INSTALL.md` (line 9), and any other file the audit of D4 finds that states the 3.10 floor as a current requirement.
- Does not touch: `MIGRATION.md` (historical release rows), `docs/specs/` locked specs, source code, tests, CI workflows (already 3.11), Sonar or GitHub settings.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The only vulnerable `pillow` resolution exists for Python 3.10. Removing 3.10 support removes it by construction and lets the conditional pillow override collapse to one entry. Rejected: leaving the gate red until `fastembed` allows Pillow 13; an exception in the Sonar UI; raising the floor beyond 3.11 (3.12.5 is allowed by the owner but nothing needs it; the 3.11 security-support end, about Oct 2027, is the trigger to raise the floor again).

## 2. Decisions

- D1. `pyproject.toml`: `requires-python = ">=3.11"`; ruff `target-version = "py311"`. Leave mypy `python_version = "3.12"`; in its comment change only the floor wording ("3.10" to "3.11"); do not restate the numpy version claims.
- D2. Replace the `[tool.uv] override-dependencies` block (two conditional pillow entries) with the single unconditional entry `"pillow>=13.0.0"`, because `fastembed` (0.9.0) still declares `pillow<13` and the override is what lets the resolver pick Pillow 13. Rewrite the `shortcut:` comment above it: ceiling = fastembed's `pillow<13` pin overridden (Pillow < 13 carries the eight advisories fixed in 13.0.0 (CVE-2026-112395, -112397, -112400, -112403, -112404, -112895, -112896 and -112897: take this list, the current comment names -112899 by mistake; image decode and save paths such as TIFF, GIF, PPM, EPS and PDF append); SALTMDB embeds text only and never calls Pillow); upgrade trigger = remove the override as soon as fastembed allows Pillow 13. Drop the sentences about Python 3.10. Architect probe (scratch, `uv lock` with these two edits) resolved cleanly.
- D3. Regenerate the lock: `uv lock` (no `--upgrade`), then `uv sync --locked --python 3.11 --extra dev`. Probe result for the expected diff: `pillow` 12.3.0 block removed (13.0.0 stays), and the Python-3.10-only duplicates removed: `numpy 2.2.6`, `onnxruntime 1.23.2`, `rpds-py 0.30.0`, `stevedore 5.8.0`, plus `mpmath`, `sympy`, `pyreadline3`, `exceptiongroup` and `tomli` (all with only `< 3.11` edges; `tomli-w` and `typing-extensions` stay, losing only their `< 3.11` edges) and every `python_full_version < '3.11'` resolution marker. No package that stays in the lock changes its version. List every difference from this expectation in the report; an unexpected changed version is reported, not kept silently.
- D4. Audit: `rg -n "3\.10|py310" --glob '!uv.lock' --glob '!MIGRATION.md' --glob '!docs/specs/*'` and fix each current-requirement statement (`INSTALL.md` line 9 becomes "Version 3.11 or higher."); also check Python classifiers and `README.md`/`CONTRIBUTING.md`. Remove any 3.10-only code path or `sys.version_info < (3, 11)` branch ONLY if the audit finds one; otherwise none.
- D5. Verification: `./verify` exits 0; `uv pip list` in the project venv shows `pillow 13.0.0`; `rg -c '^name = "pillow"' uv.lock` is 1; `pip-audit` clean (part of verify).

## 3. Changes per file

| File | Change |
|---|---|
| `pyproject.toml` | D1, D2 |
| `uv.lock` | D3 (regenerated) |
| `INSTALL.md` | D4 |

## 4. Tests

None new: no behaviour changes. `./verify` is the gate.

## 5. Documentation

`INSTALL.md` only (D4).

## 6. Out of scope

`MIGRATION.md` (the "Python 3.11+, recreate the venv" note belongs in the next release's row; the architect records it as a backlog row); Python 3.12 floor; Sonar quality-gate enforcement; the remaining S107 slices; Pillow removal (fastembed needs it).

## 7. Acceptance

1. `./verify` exits 0. 2. `git status` lists only files named in sections 0 and 3 (plus any D4 file). 3. D3 changed-versions list. 4. Architect, after CI: Sonar gate OK, `new_sca_severity_any_issue` below threshold, no open pillow items.
