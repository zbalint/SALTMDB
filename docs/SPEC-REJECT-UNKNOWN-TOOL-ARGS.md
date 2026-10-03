# SPEC-REJECT-UNKNOWN-TOOL-ARGS

## 0. Status

**LOCKED** (2026-10-03). Context: `saltmdb-feedback-a2amx-architect-2026-10-03`.

- Location/branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`. Use the repo `.venv` (`.venv/bin/python -m pytest`), never system python.
- Test seam: `tools.mcp.call_tool(name, arguments)` (the real FastMCP entry point, as already used in `tests/test_mcp_tools.py:1191`).
- Scope (may edit): `src/saltmdb/mcp/server.py`, `tests/test_mcp_tools.py` (add tests only).
- Does not touch: `src/saltmdb/mcp/tools.py`, `src/saltmdb/daemon/**` (daemon dispatch and the `hooks/` RPC path), `domain/**`, hooks, skills.
- Developer leaves everything uncommitted; architect reviews and commits.

## 1. Why

A peer architect called `search_memory` with `query="..."` (the real parameter is `query_keywords`). FastMCP built the argument model with pydantic's default `extra="ignore"`, so the unknown key was dropped, `query_keywords` became `None`, and the call silently took the no-query browse path (`orchestrator.py` ~L863: newest-first, score 0.0, cursor `offset:5`). The result looked like a normal search and the caller concluded search was insensitive to its query. Verified by probe 2026-10-03: `FastMCP.call_tool("f", {"query": "hello"})` ran with `query_keywords=None` and no error; after setting `extra="forbid"` on each tool's `fn_metadata.arg_model` and `model_rebuild(force=True)`, the same call on the real `search_memory` raised `ToolError` naming the field: `Extra inputs are not permitted` for `query`. The same silent drop affects all 24 registered tools (a wrong or stale name such as `owner_id` vanishes silently).

## 2. `src/saltmdb/mcp/server.py`

After the existing line (currently the last statement of the file, ~L66):

```python
from saltmdb.mcp import tools  # noqa: F401, E402 -- registers @mcp.tool() decorators as a side effect
```

add, using the same iteration the probe used:

```python
for _tool in mcp._tool_manager.list_tools():
    _tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    _tool.fn_metadata.arg_model.model_rebuild(force=True)
```

with a short comment saying why (a misspelled parameter name must fail loudly instead of silently becoming a browse/default call). It runs once at import, covering every tool registered by the `tools` import. Rules:

1. No wrapper function, no new module (single use, Coding Standards rule 14).
2. `_tool_manager` is a private FastMCP attribute; add `# shortcut: private FastMCP API, replace if the pinned mcp version changes it` (upgrade trigger: an `mcp` dependency bump that breaks the loop; the new test in section 3 fails loudly).
3. Do not change the error text; the default pydantic message names the offending field and is sufficient.

## 3. `tests/test_mcp_tools.py` (add only)

Add a test class (async, same style as the existing real `call_tool` tests near L1746) with:

1. `search_memory` called with `{"query": "anything"}` raises `ToolError` whose message contains `query` and `Extra inputs are not permitted`.
2. Generic: for every tool in `tools.mcp._tool_manager.list_tools()`, `arg_model.model_config["extra"] == "forbid"`.
3. Regression: `search_memory` with `{"query_keywords": "anything"}` still succeeds (use the existing fixture/DB setup of neighbouring tests).

## 4. Out of scope

- Fixing the separate relevance miss (a closely titled memory missing from a query's top 5): its own investigation, not this spec.
- Changing the browse path, adding aliases (e.g. accepting `query`), or listing valid parameter names in the error.
- Any change to the daemon RPC path (`hooks/saltmdb-stop-trace-capture.py` sends `kwargs` straight to the daemon; unaffected, confirm by not touching it).
- Hook friction and digest labels reported in the same thread.
- Renames, refactors, or reordering imports in any file.

## 5. Acceptance

Baseline at lock time (before any change), run by the architect in the main checkout with `.venv`: `.venv/bin/python -m pytest tests/test_mcp_tools.py tests/test_phase3_mcp_surface.py tests/test_phase4_mcp_surface.py tests/test_trace_mcp_tools.py tests/test_retrieve_context_wiring.py -q` -> `150 passed`. The pre-change probe above is the only new-behaviour evidence; the commands below require the new code and are run after implementation.

1. `.venv/bin/python -m pytest tests/test_mcp_tools.py -q` -> all pass, including the three new tests.
2. `.venv/bin/python -m pytest tests/test_mcp_tools.py tests/test_phase3_mcp_surface.py tests/test_phase4_mcp_surface.py tests/test_trace_mcp_tools.py tests/test_retrieve_context_wiring.py -q` -> 150 + the new tests, 0 failed.
3. Full repo test suite under `.venv` (the repo's documented command) -> no new failures; any failure must be shown to pre-exist (`git stash` comparison) or fixed in scope. If a pre-existing test or caller passes an unknown argument to a tool through `call_tool`, that is a material finding: stop, report `BLOCKED — SPEC ADJUDICATION REQUIRED` with the call; do not widen scope.
4. `git diff --stat` shows only `src/saltmdb/mcp/server.py` and `tests/test_mcp_tools.py`.
5. Report the new test count, and one manual proof: `mcp.call_tool("search_memory", {"query": "x"})` raises with `query` named.
