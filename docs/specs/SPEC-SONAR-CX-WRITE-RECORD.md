# SPEC-SONAR-CX-WRITE-RECORD

## 0. Status

LOCKED 2026-10-22 (consultant pre-lock review folded in)

- Backlog item: none (Sonar cleanup after S2, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `5f11c62`; `./verify` exits 0. CI gate on that commit is ERROR only because of one new `python:S3776` (score 22, limit 15) on `_call_and_record_write` in `src/saltmdb/mcp/tools.py`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the existing `_call_and_record_write` callers (`store_memory`, `update_memory_metadata`, `revise_memory`, `supersede_memory`, `consolidate_memories` tool functions) and the raw-MCP harness `tests/mcp_contract.py`; `tests/test_trace_mcp_tools.py` (about lines 240 and 319) calls `tools._call_and_record_write` directly with a patched trace flag and a patched `_backend_or_raise`; those tests are the best existing characterization of the trace branch and must stay green. Direct calls of `tools._call_and_record_write` with a stubbed backend are an allowed seam for D2.
- Scope (may edit): `src/saltmdb/mcp/tools.py` (only `_call_and_record_write` and the new private helpers placed next to it). Tests: only new tests in `tests/test_mcp_write_tools_v2.py` if D2 finds an uncovered branch.
- Does not touch: everything else, including the existing assertions of every test.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

S2 (commit afc7474) grew `_call_and_record_write` to three jobs in one body: wrap a string result into a rejection, rename the id key and the error field to their public names, and queue the trace write. Sonar scores it 22. The fix is a behavior-preserving split.

## 2. Decisions

- D1. Behavior is unchanged: for every input the returned value, the trace queue content, call order and the exceptions stay identical. Public names and signatures of all tools stay as they are.
- D2. Characterization first: run `tests/test_mcp_write_tools_v2.py`, `tests/test_mcp_tools.py`, `tests/test_trace_mcp_tools.py` and `tests/test_tag_merge_tool.py` under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <files>`, then `coverage report --show-missing --include=src/saltmdb/mcp/tools.py`) and list the uncovered lines and branches of `_call_and_record_write`. Add characterization tests for each uncovered branch BEFORE moving code (non-dict result, error entry that is not a dict, `data` that is not a dict, tool names outside the id-rename set).
- D3. Extract three private module-level functions next to it: `_wrap_string_result(result)` (string to `rejected([error("VALIDATION_ERROR", ...)])`, else unchanged), `_to_public_write_result(tool_name, result)` (the dict copy, the `id` to `memory_id` rename for `store_memory` and `update_memory_metadata`, the `entity_id` to `memory_id` error-field remap for `update_memory_metadata`), and `_queue_trace_write(tool_name, result)` (the trace block). `_call_and_record_write` becomes the three calls in that order. The ORDER is load-bearing: the trace step reads `_WRITTEN_ID_KEY["store_memory"] == "memory_id"`, which exists only after the public step renamed `id`; running the trace step first silently loses every store trace link. Keep these exact semantics: the `id` rename is gated on status `ok` AND a dict `data`, while the `entity_id` error-field remap is gated only on a dict result whose `errors` is a list (an `ok` envelope's `errors: []` included), as two separate conditions; both copies (`dict(result)` and the copied `data`) stay so the backend's object is never mutated; a result that is not a dict (a list, `None`) is returned untouched by the public and trace steps; the trace gate keeps its order `tool_name in _WRITTEN_ID_KEY`, then `is_trace_capture_enabled()`, then the dict and status checks, so the flag is read only for keyed tools. The helpers stay in `tools.py` and use the module-global names `is_trace_capture_enabled` and `_backend_or_raise` (tests patch them there; no local re-import). `_to_public_write_result` starts with an early `if not isinstance(result, dict): return result` instead of enclosing the body in an `if`. If the proxy still reports it above 12, a fourth private helper for the error-field remap is allowed. No new modules, no new dependency, no flag, no suppression.
- D4. The id-rename set and the remap tool name are written once as module constants if that removes a duplicated literal; do not change their values.
- D5. Measure with the proxy of section 6: every touched function reaches at most 12.

## 3. Changes per file

| File | Function | Sonar score |
|---|---|---|
| `src/saltmdb/mcp/tools.py` | `_call_and_record_write` (becomes the three calls) | 22 |
| `src/saltmdb/mcp/tools.py` | new `_wrap_string_result`, `_to_public_write_result`, `_queue_trace_write` (and the optional remap helper of D3) | new |

## 4. Tests

Per D2. Existing assertions stay as they are. Tests use the existing fixtures; never the real `~/.saltmdb` or the working daemon.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`, plus the complexity proxy (outside the repo, never in `pyproject.toml` or `uv.lock`):

```sh
SCRATCH=<a scratch directory you create under /tmp>; echo "$SCRATCH"
uv venv -q "$SCRATCH/cxvenv" && uv pip install -q --python "$SCRATCH/cxvenv/bin/python" complexipy
"$SCRATCH/cxvenv/bin/complexipy" --max-complexity-allowed 12 src/saltmdb/mcp/tools.py
```

It must report no function above 12 among the touched functions (functions that already exceeded 12 before your change are listed as pre-existing; do not touch them). Delete only under the scratch directory afterwards: `rm -rf "${SCRATCH:?}/cxvenv"` after printing the resolved path.

## 7. Out of scope

Other Sonar rules, other files, any change of behavior, and everything else in `tools.py`.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy reports no touched function above 12.
3. `git diff --stat` lists only `src/saltmdb/mcp/tools.py` and, if D2 required it, `tests/test_mcp_write_tools_v2.py`.
4. The report lists before and after proxy scores, the characterization tests added, and a mutation check (break the rename, the remap, the trace block, and the call order "trace before rename" each once in the checkout, show a test fails, restore).
5. Architect, after review and commit: CI gate OK and the S3776 key for `_call_and_record_write` gone.
