# SPEC-SONAR-UNUSED-PARAMS

## 0. Status

LOCKED 2026-10-20

- Backlog item: none (Sonar rule `python:S1172`, six open issues on `develop` `18c189d`). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Owner decision (2026-10-20): remove all six unused parameters and update every caller, including the two that carry a documented reason to stay (`transport_session_id`, `policy`).
- Baseline: `develop` at `18c189d` or later; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the six functions called directly; `tests/test_daemon_server.py` for the daemon call chain.
- Scope (may edit): the six function definitions, the four forwarding helpers of D1.1, the helper `_replacement_content` of D1.3 (amendment 1, its caller and its comment), every call site of them in `src/` and `tests/` (argument lists and test fakes' signatures only), the one assertion of D3, the `accept_or_abstain` docstring sentence about `policy` and the `_validate_caller_session` comment block (D1.6).
- Does not touch: function bodies beyond deleting the parameter and its uses, any MCP tool signature or tool schema, historical specs under `docs/specs/`, any other file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Six parameters are accepted and never read, so Sonar reports `python:S1172`. Dead parameters mislead callers about what influences the result. Rejected: keeping `policy` and `transport_session_id` because their comments explain them (the owner chose removal; the future extension is re-added when it exists), and a `noqa` or Sonar status change (owner rule: no suppressions, no issue-status changes).

## 2. Decisions

- D1. Remove these parameters from the definitions:
  1. `db_path` of `get_fresh_entity_centroids` (`src/saltmdb/domain/services/cohesion_service.py`, line 167). Its three callers in `relation_service.py` (lines 276, 2009, 2699) stop passing `db_path or get_db_path()`. The `get_db_path` import stays (still used elsewhere). Cascade: four private helpers receive `db_path` only to forward it, and would become new S1172 findings, so remove `db_path` from them too and update their call sites: `_relation_gate_measurements` (def 263, use 277) and its caller `_store_relation_write` (def 407, use 474, call sites 680/688 and 696/704); `_consolidation_measure_cohesion` (def 1970, use 2010) and its caller `_consolidation_cohesion` (def 2070, use 2084, call sites 2278/2281). The chain stops there (`bulk_commit_consolidation` and the callers of those two use `db_path` for other things). No test or script calls the four helpers directly.
  2. `title` of `evaluate_memory_quality` (`src/saltmdb/utils/nlp.py`, line 713). Its three callers (`write.py` 861, `relation_service.py` 2131, `lifecycle.py` 353) stop passing the second positional argument.
  3. `metadata` of `validate_memory_input` (`src/saltmdb/domain/services/memory_service/validation.py`, line 177). The new signature is `(title, content, tags=None)`. At `write.py` 839 delete the `metadata` positional; at `lifecycle.py` 343-348 delete the whole expression line `metadata if metadata is not None else before.get("metadata")`. The `memory_service/__init__.py` re-exports are names only and stay. Cascade (amendment 1): in `lifecycle.py`, `_replacement_content(before, title, content, tags, metadata)` (def 333) used `before` and `metadata` only in that expression, so both become unused. Make its signature `(title, content, tags)`, update its only caller at line 451 (`_replacement_content(title, content, tags)`), and correct its comment at 338-339, which wrongly says `validate_memory_input` checks "metadata shape" (it checks title bounds and tag-name shape only). The cascade stops there: `_replacement_prepare` still uses `before` and `metadata` elsewhere (`_replacement_fields` and the snapshot).
  4. `tags` of `check_duplicate_memories` (`memory_service/duplicates.py`, line 181). Only two call sites pass it: the `tags=` keyword at `write.py` 1009 (call at 1005) and at 1365 (call at 1361). `relation_service.py` 2190 passes no tags and is unchanged. KEEP the `"tags"` key in the deferred-probe request dict that `write.py` builds at 990-999 and replays at 1358: `tests/test_store_duplicate_probe_off_writer.py` 126-135 asserts the exact sink contents. Only the two keyword arguments go.
  5. `policy` of `accept_or_abstain` (`memory_service/ranking.py`, line 792). Its callers (`orchestrator.py` 334 and 422, 15 calls in `tests/test_relevance_gate.py`, one wrapper in `scripts/diagnostics/live_audit_repro.py` that forwards `*args, **kwargs`) pass only `evidence`; nothing to change at the call sites. Rewrite the docstring sentence about `policy` (lines 796-798). Keep the existing `# noqa: PLR0911` on the def line (a different rule).
  6. `transport_session_id` of `DaemonState._validate_caller_session` (`src/saltmdb/daemon/server.py`, line 471). Removing it makes the same-named parameter of `_handle_tool_call` (def 539, parameter 543) unused too, so remove that as well. Drop the third argument at the call at 567-568 and at the call at 330. The `session_id` local at 330 STAYS (line 321 passes it to `_handle_session_method`). Delete the `# noqa: ARG002` on the removed parameter and rewrite the explanatory comment block at the top of `_validate_caller_session` so it no longer refers to a parameter that is gone (keep the history in one or two sentences).
- D2. Where removing a parameter leaves a local variable or an import unused, remove that too (none expected; the consultant found none). Do not touch anything else.
- D3. Tests: update every test call site that passes a removed argument. Known sites: `tests/test_daemon_server.py` 320 (third positional argument of `_validate_caller_session`); `tests/test_cohesion_service.py` 108, 124, 144, 162, 183, 210, 224, 243, 255, 277, 297 and `tests/test_relation_service.py` 2504, 2524 (the third positional of `get_fresh_entity_centroids`); the fake signatures `(entity_ids, conn, db_path)` at `tests/test_relation_service.py` 2245, 2364, 2407 (and the inner `real_get_centroids(entity_ids, conn, db_path)` calls they forward to) and `_cohesion(self, ids, _conn, _db_path)` at `tests/test_consolidate_memories_phase4.py` 41. Do not change any assertion, with exactly one exception: `tests/test_consolidate_memories_phase4.py` 969-972 `quality.assert_called_once_with("Canonical [REDACTED_SECRET] content.", "Canonical [REDACTED_SECRET]")` drops its second argument, because the call now has one. Keep the test name (the redacted title is still produced for other purposes) and say so in the report. Mocks and patches that use `return_value` or `(*args, **kwargs)` keep working. A missed site fails with `TypeError` in `./verify`.
- D4. Behaviour is unchanged: no test outcome changes. No new Sonar finding: `ruff check --select ARG,RET,F` on every edited file reports no hit that the baseline copy does not.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/domain/services/cohesion_service.py` | `get_fresh_entity_centroids` loses `db_path` |
| `src/saltmdb/domain/services/relation_service.py` | three centroid calls; `db_path` removed from four forwarding helpers and their call sites (D1.1); one `evaluate_memory_quality` call |
| `src/saltmdb/utils/nlp.py` | `evaluate_memory_quality` loses `title` |
| `src/saltmdb/domain/services/memory_service/validation.py` | `validate_memory_input` loses `metadata` |
| `src/saltmdb/domain/services/memory_service/duplicates.py` | `check_duplicate_memories` loses `tags` |
| `src/saltmdb/domain/services/memory_service/ranking.py` | `accept_or_abstain` loses `policy`; fix its docstring sentence about `policy` |
| `src/saltmdb/domain/services/memory_service/write.py`, `lifecycle.py` | call sites |
| `src/saltmdb/daemon/server.py` | two definitions, one call, comment block |
| `tests/` | call sites and fake signatures of D3; the one assertion of D3 |

## 4. Tests

No new test: the change is signature-only and every existing test that reaches these functions keeps passing. A test that passes a removed keyword fails with `TypeError` until updated, which makes a missed call site visible in `./verify`.

## 5. Documentation

Nothing changes in `docs/`, `README.md` or `skills/`. The only quotations of a removed parameter are in historical locked specs (`SPEC-STORE-DUPLICATE-PROBE-OFF-WRITER.md` line 63, `SPEC-SONAR-PY-BEHAVIOR.md` line 42), which are not edited.

## 6. Verification procedure

`./verify`. Then `rg -n "transport_session_id" src` and `rg -n -A2 "get_fresh_entity_centroids\(" src` and read every remaining hit against D1 (the `db_path or get_db_path()` text still legitimately appears at six other lines of `relation_service.py`). Also run `ruff check --select ARG,F` on `relation_service.py` and the other edited source files and compare with the baseline copy.

## 7. Out of scope

The nine S107 functions (separate spec), the taint findings, any other unused parameter that Sonar does not currently list, MCP tool signatures.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only files named in section 3 (plus test files that called a removed argument).
3. `git diff -U0` shows no changed line other than parameter deletions, argument deletions, the two comment/docstring rewrites and unused-name cleanup of D2.
4. Mutation sample on a scratch copy: re-add one wrong argument at a changed call site and see a repo test fail with `TypeError`.
5. Architect, after commit and CI: the Sonar search shows the six S1172 issues no longer open on `develop`, with no new issue in the touched files.

## 9. Pre-lock gate notes

Consultant review (2026-10-20, AST scan of src, tests, hooks, scripts at `18c189d`) found three blockers, all folded in: the `db_path` cascade into four helpers (D1.1), the one assertion that must change (D3), and the `"tags"` key of the probe request that must stay (D1.4). Eight corrections (call-site lists, `relation_service.py` 2190 passes no tags, `session_id` stays, imports stay, docs untouched, narrower rg) are folded in. Not executed: ruff or Sonar on a modified copy; the cascade rests on the S1172 rule semantics and the existing ARG001 precedent, so the developer must run `ruff check --select ARG,F` and acceptance 5 is the real confirmation. Section 0 "Does not touch" and section 5 contradictions resolved.

Amendment 1 (2026-10-20, developer BLOCKED m_2593, verified by the architect against `lifecycle.py` 333-366 and 451): the consultant's cascade scan found the `db_path` helpers but missed that `_replacement_content` used `before` and `metadata` only in the expression D1.3 deletes. The pre-lock gate was a read of the caller chain for one parameter, not for the others; for every removed parameter the developer must check that the enclosing function still uses each of its own parameters (ruff ARG on the edited files, baseline vs after) and report any further cascade as BLOCKED rather than widening scope.
