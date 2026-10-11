# SPEC-BOOTSTRAP-SHARED-RESOLVE-CLI

## 0. Status

LOCKED 2026-10-11.

- Backlog item: BL-038. Shared `context_id`: `bootstrap-shared-resolve-cli-2026-10-11`.
- Baseline: `296f85e` on `develop`; `./verify` exit 0, 2191 passed, 1 skipped, 172 subtests (it runs
  `pytest tests/ hooks/tests/`).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `hooks/tests/test_session_start_bootstrap.py` (loads the script by file path) and
  `hooks/tests/test_stop_related_memories.py` L625 (already calls `common.resolve_cli()`).
- Scope (may edit):
  - `hooks/saltmdb-session-start-bootstrap.py` (L15-46)
  - `hooks/_saltmdb_hook_common.py` (the docstring of `resolve_cli`, L280-285 only)
  - `hooks/tests/test_session_start_bootstrap.py`
- Does not touch: `~/.agents` and `~/.claude/hooks` (a symlink to `~/.agents/hook-sources`; syncing
  the repo hooks there is the owner's deploy step), the other hooks, `hooks/README.md`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`hooks/saltmdb-session-start-bootstrap.py` carries its own `_resolve_cli()` (L28-43) with the same
precedence as `resolve_cli()` in `hooks/_saltmdb_hook_common.py` (L280-296). The shared one's
docstring says "a follow-up moves the bootstrap onto this one". Two copies can drift, and the
bootstrap is the hook whose silent failure costs the whole session digest.

The move is safe on the deployed layout: `~/.claude/hooks` is a symlink to `~/.agents/hook-sources`,
the repo's deploy step copies `_saltmdb_hook_common.py` next to every script (`hooks/README.md`
L207), the Stop hooks already import it, and the deployed copy already defines `resolve_cli`
(checked 2026-10-11 with `rg -c "def resolve_cli"`). Cost: one import of a stdlib-only module of
309 lines (`json`, `re`, `subprocess`, `sys`, `pathlib`; the bootstrap already imports four of them).

Rejected: keeping both copies and adding a test that asserts they match (still two copies);
making the bootstrap fall back to a local copy when the import fails (that is the duplicate again).

## 2. Decisions

- D1. The bootstrap defines no `_resolve_cli`; it uses `resolve_cli` from `_saltmdb_hook_common`.
- D2. The import follows the Stop hook's pattern (`hooks/saltmdb-stop-related-memories.py` L62-63):
  `sys.path.insert(0, str(Path(__file__).resolve().parent))`, then
  `from _saltmdb_hook_common import resolve_cli  # noqa: E402`.
- D3. `CLI = resolve_cli()` stays a module-level constant with the same name, so `run_cli` and the
  rest of the script do not change.
- D4. `os` and `shutil` are used only inside `_resolve_cli` (L36, L39) and are removed. `Path` stays
  (D2 needs it, and two kept tests patch `bootstrap.Path`).
- D5. No fallback or guard around the new import. If the deployed directory lacks
  `_saltmdb_hook_common.py` or holds a copy without `resolve_cli`, the hook fails loudly at import
  (the Stop hooks already carry the same dependency). A silent `CLI = None` would hide the problem.

## 3. Changes per file

### 3.1 `hooks/saltmdb-session-start-bootstrap.py`

Replace the `_resolve_cli` definition (L28-43) with the D2 import placed after the `START`/`BUDGET_S`
constants (so the budget clock starts before the import, as the Stop hooks place theirs),
and change `CLI = _resolve_cli()` (L46) to `CLI = resolve_cli()`. Apply D4 (delete the `os` and `shutil` imports).

### 3.2 `hooks/_saltmdb_hook_common.py`

Replace the last sentence of the `resolve_cli` docstring ("Same precedence as ``_resolve_cli`` in
saltmdb-session-start-bootstrap.py, which still keeps its own copy; a follow-up moves the bootstrap
onto this one.") with "The session-start bootstrap hook uses this function too."

### 3.3 `hooks/tests/test_session_start_bootstrap.py`

Its tests call `bootstrap._resolve_cli()` (L42, 49, 56, 70, 80). Change each to
`bootstrap.resolve_cli()`; keep every patch and assertion. Update the module docstring's mention of
`_resolve_cli()` to `resolve_cli()`. Add one test, right after loading the bootstrap, asserting
`bootstrap.resolve_cli is sys.modules["_saltmdb_hook_common"].resolve_cli`,
`not hasattr(bootstrap, "_resolve_cli")`, and
`Path(inspect.getfile(bootstrap.resolve_cli)).resolve() == HOOKS_DIR / "_saltmdb_hook_common.py"`.
(`_load_module` does not register modules in `sys.modules`, so do not compare against a second
`_load_module` of the common file: that is a different module object.) The kept tests that patch
`shutil.which`, `os.environ` and `bootstrap.Path.home` still reach `resolve_cli`, because it imports
`os` and `shutil` inside the function and `Path` is the same `pathlib.Path` class.

## 4. Tests

Written failing first: change the tests in 3.3 first (they fail with `AttributeError` on the old
script), then the script. Expected values are the existing literals.

## 5. Documentation

None. `hooks/README.md` already lists the common module as imported by every hook.

## 6. Verification procedure

`./verify` alone on the host. Plus, run the script itself once with the repo `.venv` python and
`SALTMDB_CLI_PATH` pointing at a scratch fake CLI that prints a line, and show it prints that line:
`SALTMDB_CLI_PATH=<scratch>/fake-cli .venv/bin/python hooks/saltmdb-session-start-bootstrap.py < /dev/null`
(the hook's stdin payload is optional; if it exits non-zero without a payload, give it `{}` on stdin).

## 7. Out of scope

Syncing to `~/.agents` (the deployed `~/.claude/hooks`, `~/.codex/hooks` and `~/.gemini/config/hooks`
are all symlinks to `~/.agents/hook-sources`, so one sync switches every harness at once; that is the
owner's deploy step); any change to the hook's budget, output or pending notice;
`docs/specs/SPEC-STOP-RELATED-MEMORIES-HOOK.md` (a locked record that mentions the old function name).

## 8. Acceptance

1. `./verify` exits 0 with the changed tests.
2. `rg -n "_resolve_cli" hooks` prints exactly the new test's `hasattr(bootstrap, "_resolve_cli")`
   assertion (section 3.3) and nothing else.
3. The section 6 run exits 0 and prints the fake CLI's line (twice, as at baseline).
4. `git diff --stat` lists only the three files in section 0 scope.

## 9. Pre-lock gate notes

- Consultant review (m_1289): F1 identity test replaced by the `sys.modules` assertion; F2 the Codex
  "variant" sentence dropped (the deployed bootstrap is one shared file); F3 ruled in D5; F4 import
  placed after the constants; F5 acceptable as written.
- Baseline probe done: the section 6 command against the unchanged script exits 0 and prints
  `FAKE-DIGEST` twice (bootstrap digest and session digest, both from the fake CLI).
- D1-D5 re-read against section 3 at lock: D4 (`os` and `shutil` removed, `Path` stays) matches 3.1.
  The spec states no Codex-variant claim: the only Codex mentions are the `~/.codex/hooks` symlink
  in section 7 and this note's record of the dropped sentence.
