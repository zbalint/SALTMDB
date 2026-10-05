# SPEC-GET-MEMORY-MEMORY-ID-ALIAS

## 0. Status

**LOCKED** (2026-10-03). Backlog item BL-001. Context: `saltmdb-bl001-get-memory-alias-2026-10-03`.

- Location/branch: main checkout `<repo>`, branch `develop`. Use the repo `.venv`, never system python.
- Test seams: `tools.get_memory(...)` as a plain function (as in `tests/test_mcp_tools.py:53`) and `tools.mcp.call_tool(name, arguments)` (the real FastMCP entry, as in `tests/test_mcp_tools.py:1219` via `_call_as`).
- Scope (may edit): `src/saltmdb/mcp/tools.py` (the `get_memory` function only, L1204-1232), `tests/test_phase3_mcp_surface.py` (one list literal, L49-52), `tests/test_mcp_tools.py` (add tests only).
- Does not touch: `src/saltmdb/daemon/**` (dispatch, protocol), `domain/**`, every other tool in `tools.py`, hooks, skills, README and other docs (a separate docs-sweep task owns those), `docs/BACKLOG.md` (architect updates it).
- Developer leaves everything uncommitted; architect reviews and commits.

## 1. Why

Agents repeatedly call `get_memory(memory_id=...)` because search results and the tool name suggest it. The real parameter is `entity_id`, and since e51eb32 (reject unknown arguments) the call hard-fails with a pydantic `Field required` / `Extra inputs are not permitted` error. The mistake is recorded more than once in SALTMDB memory. The owner decided (BL-001) to accept `memory_id` as an alias on `get_memory` only. Widening to other tools, or a "did you mean" hint instead (BL-010), is deliberately left for evidence.

This is a deliberate, narrow exception to the reject-unknown-arguments rule: `memory_id` becomes a declared parameter of `get_memory`, so the rule is not weakened for any other name or tool.

## 2. `src/saltmdb/mcp/tools.py` — `get_memory` only

Current signature (L1205): `def get_memory(entity_id: str, include_trace_provenance: bool = False) -> dict:`.

New signature, keeping the existing positional order (a positional call such as `tools.get_memory("entity-1", include_trace_provenance=True)` in `tests/test_trace_mcp_tools.py:155` must keep working) and appending the alias last:

```python
def get_memory(
    entity_id: str | None = None,
    include_trace_provenance: bool = False,
    memory_id: str | None = None,
) -> dict:
```

Body, before the existing `_backend_or_raise().call(...)`:

1. If both `entity_id` and `memory_id` are not `None` and differ, return `rejected([error(error_codes.VALIDATION_ERROR, "entity_id and memory_id are aliases; pass one, or the same value in both.", "memory_id")])`.
2. `resolved = entity_id if entity_id is not None else memory_id`. If `resolved is None`, return `rejected([error(error_codes.VALIDATION_ERROR, "entity_id is required (memory_id is accepted as an alias).", "entity_id")])`.
3. Pass `"entity_id": resolved` to the backend call. The backend payload never contains `memory_id`; `daemon/dispatch.py` and the daemon protocol are unchanged.

Equal values in both are accepted (no conflict). An empty string is not `None` and flows to the backend, which already rejects it (`dispatch._required_str`, "entity_id is required"); do not add separate empty-string handling.

Import `error` and `rejected` locally inside the function with the function-local import style already used in `tools.py` (L107 `from saltmdb.utils.envelope import error, rejected`, L545 `from saltmdb.utils import error_codes`). Do no wrapper helper; this is used once (Coding Standards 14).

Docstring (the MCP tool description): add one sentence after the first paragraph, exactly: `` `memory_id` is accepted as an alias for `entity_id`; pass one of them. `` Update the `Example:` line to keep `get_memory(entity_id="a1b2c3")`. Do not change the rejected-envelope description other than adding that the missing-argument and conflicting-alias cases return `VALIDATION_ERROR`.

Verified probe (2026-10-03, real pinned `.venv`, isolated FastMCP instance): a tool declared `(entity_id: str | None = None, include_trace_provenance: bool = False, memory_id: str | None = None)` registers with `required=None` and properties `['entity_id', 'include_trace_provenance', 'memory_id']`. Consequence: the published schema no longer marks `entity_id` required; the requirement is enforced by section 2 step 2 instead.

## 3. Tests

1. `tests/test_phase3_mcp_surface.py` L49-52: change the `get_memory` expected parameter list from `["entity_id", "include_trace_provenance"]` to `["entity_id", "include_trace_provenance", "memory_id"]`. Nothing else in that file changes. The L63 call `tools.get_memory(entity_id="memory-id")` stays valid.
2. `tests/test_mcp_tools.py` (add only), a new test class using the same setup as the neighbouring real-`call_tool` tests (L1195 `_call_as` pattern and temp DB fixtures):
   1. `call_tool("get_memory", {"memory_id": <full id of a stored memory>})` returns `status == "ok"` with `data["id"]` equal to that id.
   2. `{"entity_id": X, "memory_id": X}` (same value) returns `ok`.
   3. `{"entity_id": X, "memory_id": Y}` (different) returns `status == "rejected"` with an error whose `code` is `VALIDATION_ERROR` and `field == "memory_id"`; the backend is not called (assert via the recording backend where the fixture has one, otherwise assert the rejection only).
   4. `{}` returns `status == "rejected"` with `VALIDATION_ERROR`, `field == "entity_id"`.
   5. An unknown name still fails: `{"id": X}` raises `ToolError` containing `Extra inputs are not permitted` (the reject-unknown-arguments rule is intact for every other name).
   6. The alias resolves a short unambiguous prefix the same as `entity_id` does.

## 4. Out of scope

- Aliases on any other tool (`get_lineage`, `inspect_memory`, `get_related_memories`, `update_memory_metadata`, ...), a generic before-validator, and the "did you mean" hint (BL-010).
- Accepting `id` as another alias.
- Any change to `daemon/dispatch.py`, `daemon/protocol.py`, or the RPC `kwargs` shape.
- Documentation outside the tool docstring (README, skills, architecture docs); handled by the separate docs-sweep assignment.
- Renames, refactors, import reordering, or touching any other function in `tools.py`.

## 5. Acceptance

Baseline at lock time (architect, main checkout, `.venv`, before any change): `.venv/bin/ruff check .` passes (observed 2026-10-03: "All checks passed!"). The full `./verify` baseline was green at 883896e per the previous session's final run; the architect reruns it before assignment and records the result in the assignment event. The commands below require the new code and run after implementation.

1. `.venv/bin/python -m pytest tests/test_mcp_tools.py tests/test_phase3_mcp_surface.py tests/test_trace_mcp_tools.py tests/test_get_memory.py tests/test_entity_id_prefix_resolution.py -q` -> all pass, including the six new tests.
2. `./verify` (repo-documented full gate: ruff check, ruff format --check, mypy, bandit, pip-audit, deptry, pytest tests/ hooks/tests/) -> exit 0. Any failure must be shown to pre-exist (`git stash` comparison) or fixed in scope.
3. `git diff --stat` shows only `src/saltmdb/mcp/tools.py`, `tests/test_phase3_mcp_surface.py`, `tests/test_mcp_tools.py`.
4. `rg -n "memory_id" src/saltmdb/daemon` shows no new hit (dispatch untouched).
5. Report: new test count, `./verify` pass count, and one manual proof: `mcp.call_tool("get_memory", {"memory_id": "<id>"})` returns ok, and `{"entity_id": "a", "memory_id": "b"}` returns the rejected envelope.
