# SPEC-SONAR-SMALL-FIXES

## 0. Status

LOCKED 2026-10-20

- Backlog item: none. Sonar rules (11 issues): `python:S101` (1), `python:S5899` (2), `python:S7508` (2), `python:S2245` (1), `python:S2612` (1), `javascript:S7758` (1), `javascript:S9383` (2), `javascript:S2486` (1), open on `develop` `18c189d`. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `7eb84a6` or later; `./verify` exits 0 (2943 passed, 1 skipped, 233 subtests).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Scope (may edit): the files in section 3 only.
- Does not touch: the fence regexes in `utils/nlp.py` (separate spec), any other finding, any other file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Eleven issues (eight rules) are cleared by small, behaviour-preserving changes (or, for S2612, a real permission fix the owner wants). Owner rules: no suppression, no issue-status change. The consultant verified each site (report m_2582, corrected by m_2584, and m_2589).

## 2. Decisions

- D1. S101, `src/saltmdb/utils/process_memory.py`: rename `_PROCESS_MEMORY_COUNTERS` to `_ProcessMemoryCounters` at the class definition (21), the `POINTER(...)` use (40) and the `globals().get("_PROCESS_MEMORY_COUNTERS")` string (63); update `tests/test_process_memory.py` 75 and 78. The string lookup must change with the class. Acceptance requires `rg -n "_PROCESS_MEMORY_COUNTERS" src tests` to report nothing. Linux CI cannot exercise the Windows path (the only test is `skipUnless win32`).
- D2. S5899, `hooks/tests/test_stop_related_memories.py`: move only the two flagged methods, `calls()` (110) and `log_text()` (128), which use only `self.record` and `self.home`, into a new plain class `HookHelpers`, and make `class HookCase(HookHelpers, unittest.TestCase)`. `setUp` and every other helper (`set_rows`, `payload`, `run_hook`, `state_file`, `state`, `write_state`, `listed_ids`) stay on `HookCase`. Subclass behaviour is identical (they reach both through the MRO). Whether the mixin clears the rule is inferred from the rule text (a class with another base "might be a mixin"); the post-commit Sonar analysis confirms it. If it does not, report; do not rename or suppress.
- D3. S7508, `src/saltmdb/domain/services/lineage_assembly_service.py` 22-23 (`_sort_ancestor_nodes`): replace the nested `sorted(sorted(sorted(...)))` with `ordered = sorted(nodes, key=<id key>)`, then `ordered.sort(key=<updated_at key>, reverse=True)`, then `ordered.sort(key=<depth key>)`, then `return ordered`. Same keys (id ascending, then `updated_at` descending with None mapped to "", then depth ascending), same order of passes, same stability. Add one test to `tests/test_lineage_assembly_service.py` in the style of `test_same_depth_and_timestamp_are_tiebroken_by_ascending_id` (106, patched `_get_lineage_raw`): a depth-1 node with the OLDER `updated_at` and the HIGHER id, and a depth-2 node with the newer `updated_at` and the lower id; expect `[depth-1, depth-2]` (only the depth key gives that order). Do not merge the keys into one composite key (`updated_at` is a string or None).
- D4. S2245, `hooks/saltmdb-stop-critique-gate.py` 128: `random.randint(MIN_SESSION_CAP, MAX_SESSION_CAP)` becomes `random.SystemRandom().randint(MIN_SESSION_CAP, MAX_SESSION_CAP)` (same inclusive bounds; keep `import random`). Add one test in `hooks/tests/test_stop_critique_gate.py`, loading the hook with the existing `_load()` (line 14) and calling `get_or_roll_session_cap(tmp_path / "cap")` directly: patch `module.random.SystemRandom` to return a stub whose `randint` records its arguments and returns 3; assert `randint` was called with `(1, 4)`, the cap file holds 3, and a second call returns 3 without calling `randint` again. This pins the bounds and the `SystemRandom` switch deterministically.
- D5. S2612, `src/saltmdb/daemon/service.py` `install_service` (120-134): the unit file mode becomes `0o600` (owner decision: it should have been 0600 from the start). Create or rewrite the file race-free: `fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`, `os.fchmod(fd, 0o600)`, write through `os.fdopen(fd, "w", encoding="utf-8")`. Exact shape of the try block (dry-run returns before it and is unchanged): read `previous` as today; `changed` is "unchanged" / "updated" / "installed" as today; `if previous != unit:` then `fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` and `with os.fdopen(fd, "w", encoding="utf-8") as unit_file:` call `os.fchmod(unit_file.fileno(), 0o600)` then `unit_file.write(unit)`; after the branches an unconditional `os.chmod(path, 0o600)` (it tightens the unchanged branch and keeps the existing structure at line 134); the `except OSError` handler is unchanged. `os.fchmod` is POSIX-only, which is fine because `_platform_guard` (30-33) returns early off Linux. Tests in `tests/test_daemon_service.py`: in `test_install_service_writes_unit_and_runs_commands` (99) assert `stat.S_IMODE(os.stat(path).st_mode) == 0o600`; in `test_install_service_is_idempotent_and_updates` (133) keep the existing installed/unchanged/updated assertions untouched and add, in this order: after the first install, `os.chmod(unit, 0o644)`, install again with the same content, assert `S_IMODE == 0o600` (pins the unchanged-branch tightening); then `os.chmod(unit, 0o644)`, install with a different db path, assert `0o600` (pins the updated branch). Do NOT pre-create the unit before the first install (the existing `"installed" in first.message` assertion would then see "updated"). The directory mode is not changed. Report to the owner that existing installs are tightened only on the next `install`.
- D6. S7758, `src/saltmdb/viewer/static/viewer.js` `sessionColorClass` (781): `character.charCodeAt(0)` becomes `character.codePointAt(0)`. Colours are identical for every BMP character; only session ids with astral characters change colour. No test pins `charCodeAt` (`tests/test_viewer_frontend_contracts.py` 83-95 checks `session-color-${` and the call site only).
- D7. S9383, `viewer.js` 317 ("All sessions" button) and 365 ("Reset filters"): the bare `render();` becomes `void render();`, matching the existing calls at 305, 310 and 904. Update the two exact-string pins in `tests/test_viewer_rework_contracts.py` (350 and 388) to the new text and nothing else in that file.
- D8. S2486, `viewer.js` `copyText` catch (76-78): `catch (err) { setNotice(`Could not copy ${label.toLowerCase()}: ${err.message || String(err)}`); }`, matching the viewer's existing `setNotice(err.message || String(err))` convention (68). Delete the now-false "intentionally reduced" comment. Do not use `console` (not used anywhere in `viewer.js`). No test pins the old text.
- D9. No new Sonar finding and no suppression in any edited file.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/utils/process_memory.py`, `tests/test_process_memory.py` | D1 |
| `hooks/tests/test_stop_related_memories.py` | D2 |
| `src/saltmdb/domain/services/lineage_assembly_service.py`, `tests/test_lineage_assembly_service.py` | D3 |
| `hooks/saltmdb-stop-critique-gate.py`, `hooks/tests/test_stop_critique_gate.py` | D4 |
| `src/saltmdb/daemon/service.py`, `tests/test_daemon_service.py` | D5 |
| `src/saltmdb/viewer/static/viewer.js`, `tests/test_viewer_rework_contracts.py` | D6, D7, D8 |

## 4. Tests

New: the D3 depth-order test and the D4 spy test. Touched: the D5 mode assertions (two tests). Updated: D1 and D7 string and name changes. All others unchanged. The D5 mode assertions fail on unmodified source (`0o644`).

## 5. Documentation

Check `README.md`, `INSTALL.md` and `docs/` for a quoted unit-file mode; none is known. No release-note file is edited.

## 6. Verification procedure

`./verify`; `rg -n "_PROCESS_MEMORY_COUNTERS" src tests` (nothing); read the `git diff` of `viewer.js` in full (the viewer has no JavaScript test runner in `./verify`, so also run any viewer-contract tests by name).

## 7. Out of scope

S6019 and S8786 (separate spec `SPEC-SONAR-FENCE-SCANNER`), S107, S1172, the taint findings, S3776 findings.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only files named in section 3.
3. `rg -n "_PROCESS_MEMORY_COUNTERS" src tests` reports nothing.
4. Mutation sample on a scratch copy: change `0o600` back to `0o644` in `install_service` (a D5 mode test fails); change any of the three D3 keys or drop the depth pass (killers: id key `test_same_depth_and_timestamp_are_tiebroken_by_ascending_id`, `updated_at` key and direction `test_same_depth_tiebreak_prefers_later_updated_at`, depth key the new D3 test); change the D4 bounds or the `SystemRandom` call (the D4 spy test fails).
5. Architect, after commit and CI: the Sonar search shows the eleven issues no longer open on `develop` (S5899 and S2486 are inferred to clear; report any that remain), with no new issue in the edited files.

## 9. Pre-lock gate notes

Verified by the consultant at `18c189d` and `53c2f69` (none of these files is in the SPEC-SONAR-UNUSED-PARAMS edit set, so the lines stay valid): every site above, the pins, the absence of `console` in `viewer.js`, the absence of mode tests for the unit file. Unverified: that `systemd --user` reads a 0600 unit (from memory; same user), which Sonar variants clear S5899 and S2486, whether CI's umask matters (the mode is set explicitly, so no). Consultant review done (m_2595): two blockers (D5 test order, deterministic killers for D3/D4) and six corrections folded in. Not executed: the mutations; `tests/test_lineage_assembly_service.py` 160 is not a deterministic depth killer, hence the new D3 test. Not tested before or after: the `OSError` path of `install_service`.
