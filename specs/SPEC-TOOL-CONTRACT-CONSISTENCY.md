# SPEC-TOOL-CONTRACT-CONSISTENCY

## 0. Status

**LOCKED**

**Scope**: may edit/create:
- `src/saltmdb/utils/error_codes.py` (new file, §2)
- `src/saltmdb/utils/envelope.py` (module docstring only, §3)
- `src/saltmdb/daemon/dispatch.py` (§4)
- `src/saltmdb/mcp/tools.py` (§5, §12.2 [Amendment 1], §13)
- `src/saltmdb/domain/services/memory_service/lifecycle.py` (§6, §7.2 [Amendment 1], §8.2)
- `src/saltmdb/domain/services/memory_service/write.py` (§8.1, §12.2 [Amendment 1])
- `src/saltmdb/domain/services/memory_service/duplicates.py` (`bulk_archive_memory`'s
  `archive_memory`-shape-reading call site only, §6.3)
- `README.md` (Amendment 1 -- the two capacity-gate shape references at lines 193 and 234 only,
  updating them to the new lowercase `status: "rejected"`/`errors: [...]` shape and simplifying
  line 193's now-no-longer-exceptional capacity-gate clause; no other change to `README.md`,
  that's Spec 2's job)
- `AGENT_GUIDE.md` (Amendment 1 -- the one capacity-gate shape reference at line 225 only, same
  update as `README.md` above; no other change to `AGENT_GUIDE.md`)
- `src/saltmdb/domain/services/relation_service.py` (§7 -- `list_predicates`, `get_lineage`
  [Amendment 2: split into `_get_lineage_raw` + a thin enveloped `get_lineage`, and `analyze_lineage`'s
  own call site updated to call `_get_lineage_raw`], `get_related_memories`,
  `consolidate_memories`/`_consolidation_rejected`, `store_relation`, `invalidate_relation`,
  `bulk_store_relations` only; `analyze_dependencies` itself, and every other function in this
  1943-line file not named above, are untouched)
- `src/saltmdb/domain/services/conflict_set_service.py` (Amendment 2 -- its `get_lineage` import
  statement (line 14) and `_component_lifecycle_resolved`'s two `get_lineage(` call sites, all
  renamed to `_get_lineage_raw`, with no other change; every other function in this file is
  untouched)
- `src/saltmdb/domain/services/lineage_assembly_service.py` (Amendment 2 -- its `get_lineage`
  import statement (line 15) and `assemble_lineage`'s one `get_lineage(` call site, both renamed
  to `_get_lineage_raw`, with no other change; every other function in this file is untouched)
- `src/saltmdb/viewer/routes/entity_detail.py` (Amendment 3 -- the `get_lineage`-consuming block
  of its `get_lineage` method only, per §7.2 Step 5: the `is_rejected` check and the
  `result.get("data", {})` node-list extraction; no other line in this file, and no other method,
  is touched)
- `src/saltmdb/domain/services/memory_service/tags.py` (`search_tags` only, §9)
- `src/saltmdb/domain/services/librarian_service.py` (`merge_tags` only, §10 -- `run_librarian_now`,
  `trigger_librarian`, `merge_tags_heuristics`, `_run_maintenance_pass_impl`,
  `_run_maintenance_pass_on_connection`, and `_run_librarian_maintenance` are untouched)
- `src/saltmdb/domain/services/event_service.py` (`log_event` only, §11 -- `get_recent_events`
  and `dismiss_events` are untouched)
- `src/saltmdb/domain/services/core_governance_service.py` (`review_core_memory` and the
  capacity-gate rejection dict only, §12 -- `parse_is_core`, `reconcile_detail_relations`'s own
  logic beyond its two `store_relation`/`invalidate_relation` call sites, and every other function
  in this file are untouched)
- `src/saltmdb/domain/services/telemetry_service.py` (`classify_result`'s docstring only, §15 --
  its logic, and `record_call`, are untouched)
- `tests/test_mcp_tools.py`, `tests/test_relation_service.py`, `tests/test_lineage_assembly_service.py`,
  `tests/test_conflict_set_service.py`, `tests/test_get_memory.py` (Amendment 3 names these four
  explicitly per §7.2 Step 6, after Amendment 2's equivalent pass missed them), plus any other
  existing test file under `tests/` whose assertions depend on the exact string/bare-list/
  hand-rolled-dict return shape of a function named above, found and fixed via the §17 acceptance
  run (§14) -- no new test *files* are created by this spec; every fix is an edit to an existing
  test's assertions, imports, or mock patch targets/return values.

**Does not touch**: `src/saltmdb/daemon/server.py`, `src/saltmdb/daemon/client.py`,
`src/saltmdb/daemon/protocol.py` (verified in §4.1/§16 -- the fix requires no change to any of
these three), `src/saltmdb/domain/services/retrieve_context_service.py` (its own
`assemble_retrieve_context` algorithm, untouched -- only `dispatch.py`'s validation wrapper
around calling it changes), `src/saltmdb/domain/services/relation_service.py`'s
`analyze_dependencies` (the function `get_related_memories` wraps, itself unchanged),
`src/saltmdb/db/schema.py`, `src/saltmdb/config.py`, `src/saltmdb/viewer/**` **except**
`entity_detail.py`'s `get_lineage` method (Amendment 3, licensed above -- every other viewer file
and every other method in this one remain untouched), and
`scratch/plans/agent_api_redesign_implementation_plan_20260818.md` (referenced as prior art,
never edited). No new runtime dependency is introduced (`TypedDict` in §13 is Python's own
`typing.TypedDict`, already stdlib).

## 1. Why

zbalint's standing SALTMDB design goal: any agent must be able to use SALTMDB correctly as an
MCP server purely from the 19 exposed `mcp__saltmdb__*` tools' descriptions/schemas, without
ever needing to read the `saltmdb-usage` skill or any `AGENTS.md`/`CLAUDE.md`. On 2026-09-15,
zbalint asked Codex to test exactly that -- deliberately withholding the skill -- via SALTMDB
agent session `01a0a2a9-f479-707c-bce7-89c75b753cbb`. The resulting audit (SALTMDB memories
`841b2cfe`, `022e7c45`, `1446c55c`, `e62d47ee`) found real friction, including one confirmed,
reproducible bug: `revise_memory`'s schema marks `tags` optional (`tags: list[str] = None`), but
a real call omitting it fails with `INTERNAL_ERROR: tags is required` -- a runtime contract the
schema never disclosed. A follow-up read-only source audit (2026-09-19, HEAD `8dd9102`) traced
this to its exact root cause and found it is one symptom of a wider, pre-existing inconsistency:
of the 19 tools, only 5 (`get_memory`, `inspect_memory`, `revise_memory`, `supersede_memory`,
mostly `store_memory`) return the shared `utils/envelope.py` response shape; the other 14 return
ad hoc prose strings, bare lists, or hand-rolled dicts, with at least 5 distinct error-shape
conventions in active use. An agent cannot reliably infer "did this call succeed" from the
descriptions alone when the actual answer depends on which of 5 shapes a given tool happens to
use today.

`utils/envelope.py` (`ok()`/`rejected()`/`error()`/`warning()`) already exists, is already
tested (`tests/test_envelope.py`), and was already formally specified as the target shape for
**every** tool in `scratch/plans/agent_api_redesign_implementation_plan_20260818.md` §4.2 ("built
now, ahead of any caller, keeps every subsequent phase's response shape identical and
drift-proof"). All 9 phases of that plan landed (git history, `734f519`..`ab9c656`), but envelope
adoption itself stalled at 5/19 tools -- this spec is the deferred completion of that specific
piece of an already-approved plan, not a new design. `domain/services/telemetry_service.py`'s
own `classify_result()` docstring independently confirms the same gap from the read side: "MUST
be revisited once §4.2's envelope is actually wired into tool responses (Phase 2+)".

This spec fixes the **runtime** layer only: response envelopes, error-code taxonomy, the `tags`
bug's actual root cause, `get_related_memories`'s duplicate keys, `list_predicates`'s missing
backing data, batch-item schema typing, and three JSON-encoded-string fields. A second spec,
**Tool Description Rewrite**, follows once this one is implemented and merged, and rewrites all
19 tools' descriptions against these now-stable contracts. This spec makes no description-text
changes beyond what's needed to keep a docstring from actively contradicting a behavior/schema
change made here.

This is a one-shot breaking change (zbalint, 2026-09-19: "still in alpha, we can break it
whenever we want!") -- no compatibility shim, no dual-shape transition period.

**Scope note found during this spec's own pre-lock investigation, not part of the original
grilling record:** the locked design also called for fixing `search_tags`/`search_memory`'s
pagination signaling (no `has_more`, `search_memory`'s cursor duplicated per-item). Tracing this
during drafting found `search_memory`'s return type is a bare `list[dict]`, not a dict -- adding
a top-level `has_more` requires changing that return type to a wrapping dict, which cascades into
every internal caller of `memory_service.search_memory` across the codebase (this function is
one of the most-called in the whole service layer, including `retrieve_context_service.py`'s own
Milestone-A/D pipeline). That blast radius has not been enumerated here and is a distinct,
separately-sizable migration from everything else in this spec, all of which traces to a bounded,
enumerated set of call sites. Per this workspace's spec-writing pre-lock gate (steps 2-3: verify
scope against what an actual command/search finds, don't guess), this piece is **out of scope**
for this spec (see §16) and left for a dedicated follow-up spec once `search_memory`'s full
caller graph is mapped.

## 2. `src/saltmdb/utils/error_codes.py` (new file)

New module, sitting beside `envelope.py`, for the small set of **new**, generic codes this spec
introduces for tools that currently emit no code at all. It does **not** rename or replace any
code already in use by an already-envelope-adopted tool (`MISSING_TITLE`, `INVALID_TAGS`,
`UNKNOWN_ENTITY_ID`, `AMBIGUOUS_ID_PREFIX`, `INACTIVE_TARGET`, `TARGET_CHANGED`, `INVALID_MEMORY`,
`MEMORY_QUALITY_REJECTED`, `LIFECYCLE_WRITE_FAILED`, `RESERVED_PREDICATE`,
`LEGACY_READONLY_PREDICATE`, `NONCANONICAL_PREDICATE`, `UNKNOWN_PREDICATE`,
`CORE_CAPACITY_EXCEEDED`, `IDENTITY_IN_YAML_FRONT_MATTER` all stay exactly as-is -- they are
already specific, already tested, and renaming them for taxonomy purity alone would be exactly
the kind of unrelated churn Coding Standards rule 6 forbids). This is deliberately a **separate**
module from `daemon/protocol.py`'s `AUTH_FAILED`/`UNKNOWN_TOOL`/`MALFORMED_REQUEST`/
`INTERNAL_ERROR`/etc: those classify a **transport/RPC-level** failure before a request ever
reaches a tool's own domain logic (bad auth, unknown method, malformed frame); the codes below
classify what a **tool's own domain logic** rejected, once dispatch and the RPC layer already let
the call through.

```python
"""Canonical error codes for envelope.rejected()'s error entries (domain/validation layer).

New, generic codes for tools that emit no error code today. Distinct from daemon/protocol.py's
AUTH_FAILED/UNKNOWN_TOOL/MALFORMED_REQUEST/INTERNAL_ERROR, which classify a transport/RPC-level
failure before a request ever reaches a tool's own domain logic -- these classify what a TOOL's
own validation or business rules rejected, once dispatch already let the call through. Does not
replace any code an already envelope-adopted tool already uses (MISSING_TITLE, INVALID_TAGS,
UNKNOWN_ENTITY_ID, AMBIGUOUS_ID_PREFIX, INACTIVE_TARGET, TARGET_CHANGED, INVALID_MEMORY,
MEMORY_QUALITY_REJECTED, LIFECYCLE_WRITE_FAILED, RESERVED_PREDICATE, LEGACY_READONLY_PREDICATE,
NONCANONICAL_PREDICATE, UNKNOWN_PREDICATE, CORE_CAPACITY_EXCEEDED,
IDENTITY_IN_YAML_FRONT_MATTER) -- those are already specific and stay unchanged.
"""

VALIDATION_ERROR = "VALIDATION_ERROR"
"""A required field was missing, or a supplied field was the wrong shape/type, for a tool with
no pre-existing bespoke code for that specific field. Always carries `field` in the error entry."""

NOT_FOUND = "NOT_FOUND"
"""A referenced id (entity, tag, predicate) does not exist, for a tool with no pre-existing
bespoke "unknown X" code. Always carries `field` naming the offending parameter."""

ALREADY_DONE = "ALREADY_DONE"
"""An idempotent no-op: the requested end state already holds (already archived, already
canonical, already non-core). Not an error in the sense of "nothing happened because it
couldn't" -- nothing happened because it was already true. Callers should treat this as success,
not failure, when deciding whether to retry."""

CONFLICT = "CONFLICT"
"""A caller-supplied constraint (e.g. an ownership check) failed against the current state of
the target, for a tool with no pre-existing bespoke code for that specific conflict."""

INTERNAL_ERROR = "INTERNAL_ERROR"
"""An unexpected exception was caught at a tool's own top-level handler -- not a caller-input
problem. Distinguishes an unanticipated server-side fault from every other code above, all of
which describe an anticipated, nameable rejection reason."""
```

## 3. `src/saltmdb/utils/envelope.py`

**Lines 1-6** (module docstring), before:

```python
"""Uniform response envelope (agent API redesign plan §4.2).

Not yet wired into any MCP tool's return value -- Phase 1 only builds the shared module and
its tests. Tools adopt it incrementally as each is reshaped in later phases (§6). Building it
now, ahead of any caller, keeps every subsequent phase's response shape identical and drift-
proof: one envelope constructor, never a hand-rolled dict literal per tool.
```

After:

```python
"""Uniform response envelope (agent API redesign plan §4.2).

Wired into all 19 exposed mcp__saltmdb__* tools' return values as of
SPEC-TOOL-CONTRACT-CONSISTENCY (completing §4.2's original "every tool" intent, which had
stalled at 5/19 tools). One envelope constructor, never a hand-rolled dict literal per tool.
```

No other change to this file -- `ok()`/`rejected()`/`error()`/`warning()`/`is_ok()`/
`is_rejected()`'s implementations are already correct and already match every requirement below.

## 4. `src/saltmdb/daemon/dispatch.py` -- validation layer stops raising, starts returning

### 4.1 Verified request/response flow (why this change needs no other file)

Traced directly against the current source, both for a mutating tool (`revise_memory`, via
`coordinator.submit(f"tool:{tool}", lambda _conn: fn(**kwargs), ...)`) and a read tool (plain
`fn(**kwargs)`): `dispatch_tool()` returns whatever `fn(**kwargs)` returns or re-raises whatever
it raises, with no shape inspection either way (`dispatch.py:505-511`,`514-531`).
`daemon/server.py`'s `_handle_tool_call` (`server.py:542-546`) does
`result = dispatch_tool(...); return protocol.build_ok_response(request_id, result)` inside a
bare `try/finally` -- **any** returned value, including a dict shaped
`{"status": "rejected", ...}`, is wrapped as an RPC-level **success** (`ok: true`) whose
`result` is that dict; only a **raised** exception reaches `handle_request`'s outer
`except Exception as e: return protocol.build_error_response(request_id, protocol.INTERNAL_ERROR,
str(e))` (`server.py:333-335`) -- this is the exact mechanism that turns dispatch.py's bare
`ValueError("tags is required")` into the client-visible `"INTERNAL_ERROR: tags is required"`.
`daemon/client.py`'s `call_method()` (`client.py:415-442`) returns `response.get("result")`
verbatim on `ok: true`, and only raises `DaemonRpcError` on `ok: false`.
`mcp/tools.py`'s `RpcBackend.call()` (`tools.py:185-239`) only intercepts `DaemonRpcError` for
`MID_CALL_FAILURE` retry logic; every other case just returns `daemon_client.call(...)`'s result
straight through. `DirectDispatchBackend.call()` (`tools.py:173-176`, used by
`daemon/server.py`'s own in-process dispatch and by tests) does
`return dispatch.DISPATCH_TABLE[tool_name](**kwargs)` -- again, no shape inspection.

**Conclusion, verified end to end**: a dispatch function that **returns**
`envelope.rejected([...])` instead of raising flows untouched through every layer above,
arriving at the calling agent exactly as `{"status": "rejected", "errors": [...], "warnings":
[]}` -- identical in shape to how `revise_memory`'s other, already-envelope-adopted validation
failures already behave. **No change is needed to `mcp/tools.py`'s backend classes,
`daemon/server.py`, `daemon/client.py`, or `daemon/protocol.py`** for this piece. A dispatch
function that still needs to signal a genuine, unanticipated fault (a real bug, a DB error) keeps
raising -- `handle_request`'s existing catch-all continues to produce `INTERNAL_ERROR` for those,
correctly, since those are not caller-input problems.

### 4.2 The mechanical transformation, applied to every validation helper

**Before** (current, lines 35-134):

```python
def _optional_bool(kw: dict[str, Any], key: str, default: bool) -> bool:
    value = kw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value
```

*(and identically for `_optional_int`, `_optional_int_or_none`, `_optional_float_or_none`,
`_optional_scope`, `_optional_mode`, `_optional_strategy`, `_optional_direction`, `_required_str`,
`_required_str_list`, `_optional_tag_operator`, `_required_list` -- every one of these 12 helpers
raises `ValueError` on bad input today.)*

**After**: each helper raises a new `_DispatchValidationError` (a thin, module-local exception
carrying an `envelope.rejected()` payload) instead of a bare `ValueError`, and every
`_dispatch_*` function that calls one or more of them catches it at its own top level and
returns the payload directly. This keeps every existing call site's *call shape* identical
(`_required_str(kw, "entity_id")` etc. -- no call site needs its arguments changed) while fixing
what happens on failure. Add, near the top of the file, after the existing helper functions:

```python
class _DispatchValidationError(Exception):
    """Raised by the _required_*/_optional_* helpers below on caller-input validation failure.
    Every _dispatch_* function catches this at its own top level and returns its .payload
    directly (an envelope.rejected() dict) instead of letting it propagate -- propagating would
    make daemon/server.py's generic exception handler misreport it as INTERNAL_ERROR, exactly
    the bug this spec fixes. Never raised for anything other than a caller-input shape problem;
    a genuine internal fault still raises ValueError/RuntimeError/etc. and is meant to propagate."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload)
        self.payload = payload
```

Then, each helper's `raise ValueError(...)` becomes
`raise _DispatchValidationError(rejected([error(error_codes.VALIDATION_ERROR, "<same message
text as today>", key)]))` (import `rejected`, `error` from `saltmdb.utils.envelope` and
`error_codes` from `saltmdb.utils` at the top of the file). Concretely, for the two shown above
and `_required_str` (representative of the pattern for all 12):

```python
def _optional_bool(kw: dict[str, Any], key: str, default: bool) -> bool:
    value = kw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be a boolean", key)])
        )
    return value


def _required_str(kw: dict[str, Any], key: str) -> str:
    value = kw.get(key)
    if not isinstance(value, str) or not value:
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} is required", key)])
        )
    return value
```

Apply the identical substitution (same message text preserved verbatim, only the raise mechanism
changes) to: `_optional_int`, `_optional_int_or_none`, `_optional_float_or_none`,
`_optional_scope`, `_optional_mode`, `_optional_strategy`, `_optional_direction`,
`_required_str_list`, `_optional_tag_operator`, `_required_list`. `_optional_bool`/`_required_str`
above are fully worked; the other 8 differ only in their message text, already shown verbatim in
the current file (§ "Before" listing above covers all 12 by cross-reference to the unchanged
current source).

**Every one of the following `_dispatch_*` functions calls at least one of these helpers and
must catch `_DispatchValidationError` at its own top level.** This is every function in the file
that currently can raise from one of these helpers -- exhaustive, not illustrative:
`_dispatch_store_memory`, `_dispatch_search_memory`, `_dispatch_get_memory`,
`_dispatch_inspect_memory`, `_dispatch_archive_memory`, `_dispatch_manage_relation`,
`_dispatch_replacement` (shared by `_dispatch_revise_memory`/`_dispatch_supersede_memory`),
`_dispatch_consolidate_memories`, `_dispatch_review_core_memory`,
`_dispatch_update_memory_metadata`, `_dispatch_get_related_memories`. Two functions in this same
list need one further, separate fix beyond this section's own try/except wrapping, because each
also has its own bespoke inline `raise ValueError(...)` that does not go through any of the 12
helpers above: `_dispatch_get_lineage` (see §4.4) and `_dispatch_retrieve_context` (see §4.5).

The catching pattern, shown on `_dispatch_get_memory` (before/after) and then applied identically
to every function named above:

Before:

```python
def _dispatch_get_memory(**kw):
    """..."""
    entity_id = _required_str(kw, "entity_id")
    fetch = getattr(memory_service, "get_memory", None)
    if fetch is not None:
        return fetch(entity_id=entity_id)
    return memory_service.fetch_memory_chunk(entity_id=entity_id)
```

After:

```python
def _dispatch_get_memory(**kw):
    """..."""
    try:
        entity_id = _required_str(kw, "entity_id")
    except _DispatchValidationError as exc:
        return exc.payload
    fetch = getattr(memory_service, "get_memory", None)
    if fetch is not None:
        return fetch(entity_id=entity_id)
    return memory_service.fetch_memory_chunk(entity_id=entity_id)
```

For a function that calls several helpers before doing any real work (e.g.
`_dispatch_search_memory`, `_dispatch_get_related_memories`), wrap the whole block of helper
calls in one `try`, not one `try` per call -- the first validation failure should short-circuit
the rest exactly as a raised exception would have. Example, `_dispatch_get_related_memories`:

Before:

```python
def _dispatch_get_related_memories(**kw):
    entity_id = _required_str(kw, "entity_id")
    max_depth = _optional_int(kw, "max_depth", 5)
    direction = _optional_direction(kw)
    related_kwargs = {
        "entity_id": entity_id,
        "max_depth": max_depth,
        "direction": direction,
    }
    if "include_inspect" in kw:
        related_kwargs["include_inspect"] = _optional_bool(kw, "include_inspect", False)
    ...
```

After:

```python
def _dispatch_get_related_memories(**kw):
    try:
        entity_id = _required_str(kw, "entity_id")
        max_depth = _optional_int(kw, "max_depth", 5)
        direction = _optional_direction(kw)
        related_kwargs = {
            "entity_id": entity_id,
            "max_depth": max_depth,
            "direction": direction,
        }
        if "include_inspect" in kw:
            related_kwargs["include_inspect"] = _optional_bool(kw, "include_inspect", False)
    except _DispatchValidationError as exc:
        return exc.payload
    ...
```

Apply this same "wrap every helper call up to the first real side-effecting line in one `try`"
shape to each of the 12 functions listed above. `_dispatch_manage_relation` and
`_dispatch_consolidate_memories` call `_required_list` inside their own branches (`kw.get(...)`
truthiness checks) -- wrap each branch's own helper call(s), not the whole function, so a
`_DispatchValidationError` from one branch's `_required_list` call returns immediately without
touching the other branches' logic.

### 4.3 `_dispatch_replacement` -- tags becomes optional (Q1), composed with §4.2's pattern

Before (current, lines 267-293):

```python
def _dispatch_replacement(**kw):
    """..."""
    required = ("entity_id", "title", "content", "reason")
    for key in required:
        _required_str(kw, key)
    tags = _required_str_list(kw, "tags")
    if not tags:
        raise ValueError("tags is required")
    service_name = kw.pop("_service_name")
    service = getattr(memory_service, service_name)
    return service(
        entity_id=kw["entity_id"],
        title=kw["title"],
        content=kw["content"],
        tags=tags,
        reason=kw["reason"],
        owner_id=kw.get("owner_id"),
        context_id=kw.get("context_id"),
        scope=kw.get("scope"),
        memory_type=kw.get("memory_type"),
        agent_session_id=kw.get("agent_session_id"),
        repoint_relations=bool(kw.get("repoint_relations", False)),
    )
```

After:

```python
def _dispatch_replacement(**kw):
    """..."""
    try:
        required = ("entity_id", "title", "content", "reason")
        for key in required:
            _required_str(kw, key)
        tags = kw.get("tags")
        if tags is not None:
            tags = _required_str_list(kw, "tags")
    except _DispatchValidationError as exc:
        return exc.payload
    service_name = kw.pop("_service_name")
    service = getattr(memory_service, service_name)
    return service(
        entity_id=kw["entity_id"],
        title=kw["title"],
        content=kw["content"],
        tags=tags,
        reason=kw["reason"],
        owner_id=kw.get("owner_id"),
        context_id=kw.get("context_id"),
        scope=kw.get("scope"),
        memory_type=kw.get("memory_type"),
        agent_session_id=kw.get("agent_session_id"),
        repoint_relations=bool(kw.get("repoint_relations", False)),
    )
```

`tags` is no longer required to be present at all; when the caller omitted it,
`kw.get("tags")` is `None` (see §5 for why it is `None` here and not `[]`) and passes through to
`memory_service.revise_memory`/`supersede_memory` unchanged. When the caller *did* supply a
value, it is still validated as a list of strings via the now-`_DispatchValidationError`-raising
`_required_str_list` -- a malformed non-`None` value (e.g. a list containing a non-string) is
still rejected, just via the new mechanism. The old `if not tags: raise ValueError("tags is
required")` line is deleted outright: an explicitly-empty list is no longer special-cased here at
all -- it flows through to `_validate_replacement_inputs` (§6.1), which is the single place that
now decides what an empty (but non-`None`) list means.

### 4.4 `_dispatch_get_lineage`'s own inline `direction` check

**Pre-lock gate finding**: running this spec's own §17 acceptance grep (`rg -n 'raise
ValueError' src/saltmdb/daemon/dispatch.py`) against the current tree surfaces line 393's
`raise ValueError("direction must be 'ancestors' or 'descendants'")` inside
`_dispatch_get_lineage` -- a bespoke inline check, not a call to any of the 12 helpers named in
§4.2, so wrapping the function's helper calls in `try/except _DispatchValidationError` (as
§4.2's general pattern does for every other function in the exhaustive list) does **not** cover
this specific line; it needs the identical treatment given to `_dispatch_retrieve_context`'s own
inline check in §4.5 below.

Before (lines 389-394):

```python
def _dispatch_get_lineage(**kw):
    entity_id = _required_str(kw, "entity_id")
    direction = kw.get("direction") or "ancestors"
    if direction not in {"ancestors", "descendants"}:
        raise ValueError("direction must be 'ancestors' or 'descendants'")
    max_depth = _optional_int(kw, "max_depth", 5)
```

After:

```python
def _dispatch_get_lineage(**kw):
    try:
        entity_id = _required_str(kw, "entity_id")
        direction = kw.get("direction") or "ancestors"
        if direction not in {"ancestors", "descendants"}:
            return rejected(
                [error(error_codes.VALIDATION_ERROR, "direction must be 'ancestors' or 'descendants'", "direction")]
            )
        max_depth = _optional_int(kw, "max_depth", 5)
    except _DispatchValidationError as exc:
        return exc.payload
```

(`rejected`/`error` imported once at this file's top per §4.2; `error_codes` likewise.) This
duplicates `get_lineage`'s own domain-layer `direction` check (§7.2) -- both now correctly return
a `rejected()` envelope, so the duplication is harmless (dispatch.py's own check simply fires
first, identically shaped to what the domain layer would have produced anyway), and removing the
redundant check entirely is a separate simplification this spec does not attempt (Coding
Standards rule 6 -- do not touch working logic beyond what this spec's own scope requires).

### 4.5 `_dispatch_retrieve_context`'s inline check

Before (lines 447-450):

```python
def _dispatch_retrieve_context(**kw):
    query = kw.get("query")
    if not isinstance(query, str):
        raise ValueError("query is required")
```

After:

```python
def _dispatch_retrieve_context(**kw):
    query = kw.get("query")
    if not isinstance(query, str):
        return rejected([error(error_codes.VALIDATION_ERROR, "query is required", "query")])
```

(No `try`/`_DispatchValidationError` needed here specifically since it's a single inline check
with no other helper calls in this function, but the effect -- return, don't raise -- is
identical.)

## 5. `src/saltmdb/mcp/tools.py`

### 5.1 `_replacement_payload` -- preserve the omitted/empty distinction for `tags`

Before (lines 872-884):

```python
    return {
        "entity_id": entity_id,
        "title": title,
        "content": content,
        "tags": _normalize_list_or_str(tags),
        "reason": reason,
        "owner_id": owner_id,
        "context_id": context_id,
        "scope": scope,
        "memory_type": memory_type,
        "repoint_relations": repoint_relations,
    }
```

After:

```python
    return {
        "entity_id": entity_id,
        "title": title,
        "content": content,
        "tags": None if tags is None else _normalize_list_or_str(tags),
        "reason": reason,
        "owner_id": owner_id,
        "context_id": context_id,
        "scope": scope,
        "memory_type": memory_type,
        "repoint_relations": repoint_relations,
    }
```

This is the root of the whole chain: today, `_normalize_list_or_str(None)` returns `[]`
(confirmed, `tools.py:48-49`), which is why an *omitted* `tags` parameter (Python default `None`
on `revise_memory`/`supersede_memory`'s own signatures, `tools.py:892,946`) arrives at
`daemon/dispatch.py` as `[]`, not `None` -- indistinguishable from a caller explicitly passing
`tags=[]`, and it is specifically this `[]` that trips `if not tags: raise ValueError("tags is
required")` (§4.3's "before"). This one-line change is scoped to `_replacement_payload` only --
`_normalize_list_or_str` itself is unchanged and untouched, since it is also called by
`merge_tags`, `store_memory`, `consolidate_memories`, and `manage_relation`'s own tag/id-list
parameters (`tools.py:340,428,562,697,822,826,827`), none of which should gain "omission means
something different from an empty list" semantics as a side effect of this fix.

### 5.2 `store_memory`'s `is_core` parse-failure fallback

Before (lines 456-459):

```python
    try:
        is_core_ = core_governance_service.parse_is_core(is_core)
    except ValueError as e:
        return f"Error: {e}"
```

After:

```python
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error, rejected

    try:
        is_core_ = core_governance_service.parse_is_core(is_core)
    except ValueError as e:
        return rejected([error(error_codes.VALIDATION_ERROR, str(e), "is_core")])
```

### 5.3 `_predicate_disposition_error` and its two call sites -- no change needed

Confirmed already correct: `_predicate_disposition_error` (`tools.py:580-645`) already returns
`{"code", "message", "field"}` dicts, and both its call sites inside `manage_relation`
(`tools.py:737-756`, per-item bulk branch and single-item branch) already wrap them with
`rejected([env_error(...)])`. This is the pattern every other tool in this spec is being brought
up to -- cited here as a working reference, not a change target.

## 6. `src/saltmdb/domain/services/memory_service/lifecycle.py`

### 6.1 `_validate_replacement_inputs` -- `tags=None` means "inherit," not "invalid"

Before (lines 130-137):

```python
    if (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        return _replacement_error(
            "INVALID_TAGS", "tags must be a non-empty list of non-empty strings.", "tags"
        )
```

After:

```python
    if tags is not None and (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        return _replacement_error(
            "INVALID_TAGS", "tags must be a non-empty list of non-empty strings.", "tags"
        )
```

`tags is None` now short-circuits this whole check (skips it entirely) -- matching exactly how
`scope is not None and ...`/`memory_type is not None and ...` already gate the two checks
immediately below this one (lines 138-145, unchanged). An explicitly-supplied empty list or a
list containing a non-string/blank entry is still `INVALID_TAGS`, unchanged.

### 6.2 `_replacement_operation` -- resolve inherited tags, thread through `inherited`/`changed`

**Line ~298**, before:

```python
    tags = cast(list[str], tags)
```

After: delete this line. `tags` may legitimately be `None` at this point now; casting it to
`list[str]` would be a lie the type checker should not be told.

**After `before = _replacement_snapshot(conn, resolved_id)` (currently line 347)**, insert:

```python
        from . import tags as tag_ops

        inherited_tags = tag_ops.list_entity_tags(conn, resolved_id) if tags is None else None
```

`tag_ops.list_entity_tags` already exists and is already used by this exact module for the
identical purpose (`get_memory`'s own tags field, line 795) -- reused verbatim, not reinvented.
Read at this same pre-lock point in the function as `before` itself (line 347), matching the
timing already used for `owner_id`/`context_id`/`scope`/`memory_type`'s own inheritance (lines
375-379, also read from pre-lock state and not re-verified inside the write lock). This is a
structural parity choice, not an oversight: `tags` lives in a separate `entity_tags` junction
table, not a column on `entities`, so it cannot be added to the frozen-column re-check list
(lines 445-457, which diffs `PRAGMA table_info(entities)` columns only) the way the other four
inherited fields are -- but a caller-supplied `tags` value was never re-checked against
concurrent state either (it flows straight from the request), so the inherited case has exactly
the same staleness exposure the explicit case already had, not a new or worse one.

**Lines 391-395 (`changed` dict construction)**, before:

```python
        changed: dict[str, Any] = {
            "title": new_title,
            "tags": list(tags),
            "reason": reason.strip(),
        }
        changed.update(
            {
                field: value
                for field, value, supplied in (
                    ("owner_id", inherited_owner, owner_id is not None),
                    ("context_id", inherited_context, context_id is not None),
                    ("scope", inherited_scope, scope is not None),
                    ("memory_type", inherited_type, memory_type is not None),
                    ("metadata", inherited_metadata, metadata is not None),
                )
                if supplied
            }
        )
```

After:

```python
        effective_tags = inherited_tags if tags is None else tags
        changed: dict[str, Any] = {
            "title": new_title,
            "reason": reason.strip(),
        }
        changed.update(
            {
                field: value
                for field, value, supplied in (
                    ("tags", list(effective_tags), tags is not None),
                    ("owner_id", inherited_owner, owner_id is not None),
                    ("context_id", inherited_context, context_id is not None),
                    ("scope", inherited_scope, scope is not None),
                    ("memory_type", inherited_type, memory_type is not None),
                    ("metadata", inherited_metadata, metadata is not None),
                )
                if supplied
            }
        )
```

**Lines 381-390 (`inherited` dict construction)**, before:

```python
        inherited: dict[str, Any] = {
            field: value
            for field, value, supplied in (
                ("owner_id", inherited_owner, owner_id is not None),
                ("context_id", inherited_context, context_id is not None),
                ("scope", inherited_scope, scope is not None),
                ("memory_type", inherited_type, memory_type is not None),
            )
            if not supplied
        }
```

After:

```python
        inherited: dict[str, Any] = {
            field: value
            for field, value, supplied in (
                ("tags", inherited_tags, tags is not None),
                ("owner_id", inherited_owner, owner_id is not None),
                ("context_id", inherited_context, context_id is not None),
                ("scope", inherited_scope, scope is not None),
                ("memory_type", inherited_type, memory_type is not None),
            )
            if not supplied
        }
```

**Note this ordering**: `effective_tags` (used by `changed`) must be computed before `changed`'s
own dict-comprehension runs, and `inherited`'s comprehension (which only reads `inherited_tags`,
already computed at the top of §6.2) can stay before or after -- in the actual file, move the
`effective_tags = ...` line to appear immediately above `inherited`'s existing position (currently
line 381), so both dict constructions can reference their respective variables without forward
reference. This is a pure ordering adjustment; neither dict's *content* depends on this ordering.

**Line 500** (`_insert_replacement_tags(c, new_id, tags, inherited_owner)`), before:

```python
            _insert_replacement_tags(c, new_id, tags, inherited_owner)
```

After:

```python
            _insert_replacement_tags(c, new_id, effective_tags, inherited_owner)
```

**Worked example** (pre-lock gate step 8, tracing decision 1/Q1 and decision 4/Q10 together
through one concrete call): `revise_memory(entity_id="X", title="T", content="C", reason="R")` --
`tags` omitted, predecessor `X` currently has tags `["alpha", "beta"]`. `content_error` is `None`
(both `content`/`content_file_path` resolved fine, unrelated to this fix). `_replacement_payload`
sends `"tags": None` (§5.1). `_dispatch_replacement`: all four `required` fields present and
non-empty, no `_DispatchValidationError`; `tags = kw.get("tags")` is `None`, the `if tags is not
None` guard skips `_required_str_list` entirely -- `tags=None` reaches
`memory_service.revise_memory` (§6.2's entry point). `_validate_replacement_inputs`: `tags is
None` short-circuits the tags check (§6.1) -- no rejection. `_replacement_operation`:
`inherited_tags = tag_ops.list_entity_tags(conn, "X") == ["alpha", "beta"]`; `effective_tags =
["alpha", "beta"]`; `changed = {"title": "T", "reason": "R"}` (no `"tags"` key -- not supplied);
`inherited = {"tags": ["alpha", "beta"], ...}` (whichever of owner_id/context_id/scope/
memory_type were also omitted). The new entity's `entity_tags` rows are written from
`effective_tags`, i.e. it keeps `["alpha", "beta"]`. Response: `envelope_ok({..., "inherited":
{"tags": ["alpha", "beta"], ...}, "changed": {"title": "T", "reason": "R", ...}, ...})`. No
`ValueError`, no `INTERNAL_ERROR`, no dispatch-layer rejection -- the bug is fixed, and both
locked requirements (tags-inherits-when-omitted, dispatch-returns-not-raises) composed correctly
for this concrete case with no conflict.

**Second worked instance, same call but predecessor `X` also missing `title`** (i.e. caller
supplies `title=""`): `_required_str(kw, "title")` inside `_dispatch_replacement`'s existing
`for key in required` loop raises `_DispatchValidationError` (§4.2) *before* the tags branch is
ever reached (Python's `for` loop order: `entity_id`, `title`, `content`, `reason`, in that
literal order) -- returns immediately with a `VALIDATION_ERROR` for `title`, never evaluating
`tags` at all. This is deterministic (dict/tuple iteration order is guaranteed in Python) and
matches today's behavior exactly (the `for key in required` loop already short-circuits on the
first failure) -- this spec changes *how* the failure is reported, not the order fields are
checked in.

### 6.3 `archive_memory` -- envelope conversion

Before (lines 1155-1204):

```python
def archive_memory(  # noqa: PLR0911
    entity_id: str = None,
    owner_id: str = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """..."""
    if not entity_id:
        return "Error: entity_id parameter is mandatory."
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        resolved_id = resolve_entity_id(conn, entity_id)
        if not resolved_id:
            return f"Error: Could not resolve entity '{entity_id}'"

        cursor = conn.execute(
            "SELECT owner_id, scope, status FROM entities WHERE id = ?", (resolved_id,)
        )
        row = cursor.fetchone()
        if not row:
            return f"Error: Memory '{resolved_id}' not found."

        existing_owner, scope, status = row
        if status == "archived":
            return f"Memory '{resolved_id}' is already archived."
        if owner_id and existing_owner and existing_owner != owner_id:
            return f"Error: Memory '{resolved_id}' owner mismatch."

        if _in_transaction:
            _archive_entity_unchecked(conn, resolved_id)
        else:

            def _write(c):
                _archive_entity_unchecked(conn, resolved_id)

            write_transaction_retrying(conn, _write)

        return f"Memory '{resolved_id}' was successfully archived."
    except Exception as e:
        logger.error("Error archiving memory: %s", e)
        return f"Error archiving memory: {e}"
    finally:
        if should_close:
            close_connection(conn)
```

After:

```python
def archive_memory(  # noqa: PLR0911
    entity_id: str = None,
    owner_id: str = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> dict:
    """..."""
    from saltmdb.utils import error_codes

    if not entity_id:
        return rejected(
            [envelope_error(error_codes.VALIDATION_ERROR, "entity_id parameter is mandatory.", "entity_id")]
        )
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        resolved_id = resolve_entity_id(conn, entity_id)
        if not resolved_id:
            return rejected(
                [envelope_error(error_codes.NOT_FOUND, f"Could not resolve entity '{entity_id}'", "entity_id")]
            )

        cursor = conn.execute(
            "SELECT owner_id, scope, status FROM entities WHERE id = ?", (resolved_id,)
        )
        row = cursor.fetchone()
        if not row:
            return rejected(
                [envelope_error(error_codes.NOT_FOUND, f"Memory '{resolved_id}' not found.", "entity_id")]
            )

        existing_owner, scope, status = row
        if status == "archived":
            return envelope_ok(
                {"id": resolved_id, "message": f"Memory '{resolved_id}' is already archived."},
                warnings=[envelope_warning(error_codes.ALREADY_DONE, "Memory was already archived.")],
            )
        if owner_id and existing_owner and existing_owner != owner_id:
            return rejected(
                [envelope_error(error_codes.CONFLICT, f"Memory '{resolved_id}' owner mismatch.", "owner_id")]
            )

        if _in_transaction:
            _archive_entity_unchecked(conn, resolved_id)
        else:

            def _write(c):
                _archive_entity_unchecked(conn, resolved_id)

            write_transaction_retrying(conn, _write)

        return envelope_ok({"id": resolved_id, "message": f"Memory '{resolved_id}' was successfully archived."})
    except Exception as e:
        logger.error("Error archiving memory: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
    finally:
        if should_close:
            close_connection(conn)
```

`envelope_warning` is `from saltmdb.utils.envelope import warning as envelope_warning` (add to
this file's existing `from saltmdb.utils.envelope import error as envelope_error, ok as
envelope_ok, rejected` import line, currently line 23). The already-archived case becomes
`envelope_ok(...)` with a warning, not `rejected()` -- it is not an error (nothing the caller did
was wrong; the end state they wanted already holds), matching `ALREADY_DONE`'s own documented
semantics in `error_codes.py` (§2).

**Cascading call site**: `src/saltmdb/domain/services/memory_service/duplicates.py:305-311`
(`bulk_archive_memory`'s per-item loop) currently does:

```python
                res = lifecycle.archive_memory(
                    entity_id=eid, owner_id=owner, db_connection=conn, _in_transaction=True
                )
                if res.startswith("Error"):
                    raise RuntimeError(f"Bulk archive aborted (all-or-nothing): {res}")
                results.append({"status": "success", "entity_id": eid, "result": res})
```

becomes:

```python
                res = lifecycle.archive_memory(
                    entity_id=eid, owner_id=owner, db_connection=conn, _in_transaction=True
                )
                if envelope.is_rejected(res):
                    raise RuntimeError(
                        f"Bulk archive aborted (all-or-nothing): {res['errors'][0]['message']}"
                    )
                results.append({"status": "success", "entity_id": eid, "result": res["data"]["message"]})
```

(`import saltmdb.utils.envelope as envelope` at the top of `duplicates.py`, or
`from saltmdb.utils.envelope import is_rejected` -- match whatever import style the rest of that
file already uses for its other imports; read the file's existing import block before adding
this one, per Coding Standards rule 2.)

### 6.4 `update_memory_metadata` -- envelope conversion

Before (lines 954-1024, full function): returns bare strings throughout --
`"Error: entity_id is mandatory."`, `"Error: metadata must be an object (dict)."`,
`f"Error: ID prefix '{entity_id}' matches multiple memories; ..."`,
`f"Error: No memory matches entity_id '{entity_id}'."`,
`result_holder["msg"] = f"Error: Memory '{resolved_id}' not found."` (set inside `_write`),
`result_holder["msg"] = f"Memory '{resolved_id}' metadata updated (...)."` or `"... unchanged
(empty patch)."` on success, and a final fallback `f"Error: metadata update for '{resolved_id}'
did not complete."`.

After: apply the identical pattern already shown in full for `archive_memory` (§6.3) --
`VALIDATION_ERROR` for the two mandatory-field checks, `AMBIGUOUS_ID_PREFIX` (reuse this
existing, already-specific code, matching `_replacement_operation`'s own use of it at line 311)
for the multi-match case, `NOT_FOUND` for both not-found cases (the pre-lock check and the
inside-`_write` re-check), `envelope_ok({"id": resolved_id, "message": ...})` for both success
messages (empty-patch is success, not a warning -- nothing was rejected, the caller's own patch
was genuinely empty), and `INTERNAL_ERROR` for the final "did not complete" fallback (this branch
is unreachable in practice today -- `_write`'s own two paths always set `result_holder["msg"]` --
but is preserved as a defensive `INTERNAL_ERROR` rather than deleted, since removing dead
defensive code is out of scope for a contract-consistency spec).

**No cascading call site**: `grep -rn "update_memory_metadata(" src/` (run this exact command
during implementation to confirm) finds only `daemon/dispatch.py`'s own
`_dispatch_update_memory_metadata` and `mcp/tools.py`'s own `update_memory_metadata` tool wrapper
-- no other internal caller depends on this function's string return shape.

## 7. `src/saltmdb/domain/services/relation_service.py`

### 7.1 `list_predicates` -- add real disposition data (Q6/decision 6)

Before (lines 85-113):

```python
def list_predicates(
    query: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> list:
    """..."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
    try:
        if query:
            cursor = conn.execute(
                "SELECT id, name FROM predicates WHERE canonical_id IS NULL AND name LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            )
        else:
            cursor = conn.execute(
                "SELECT id, name FROM predicates WHERE canonical_id IS NULL LIMIT ?", (limit,)
            )
        return [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]
    except Exception as e:
        logger.error("Error fetching canonical predicates: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
```

After:

```python
def list_predicates(
    query: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> dict:
    """..."""
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected
    from saltmdb.utils.predicate_vocabulary import classify_predicate

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
    try:
        if query:
            cursor = conn.execute(
                "SELECT id, name FROM predicates WHERE canonical_id IS NULL AND name LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            )
        else:
            cursor = conn.execute(
                "SELECT id, name FROM predicates WHERE canonical_id IS NULL LIMIT ?", (limit,)
            )
        rows = []
        for r in cursor.fetchall():
            disposition = classify_predicate(r[1])
            rows.append(
                {
                    "id": r[0],
                    "name": r[1],
                    "disposition": disposition.status,
                    "lifecycle_tool": disposition.lifecycle_tool,
                }
            )
        return envelope_ok(rows)
    except Exception as e:
        logger.error("Error fetching canonical predicates: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
    finally:
        if should_close:
            close_connection(conn)
```

`classify_predicate` is pure, zero-DB-access, already imported this same way elsewhere
(`mcp/tools.py:589`) -- reused verbatim per Coding Standards rule 1/16. `disposition.canonical`
and `.swap` are not surfaced here: this query only ever returns rows already in the canonical
`predicates` table (`WHERE canonical_id IS NULL`), so `classify_predicate` will only ever report
`"selectable"`, `"reserved"`, or `"legacy_readonly"` for a row this query can return -- never
`"alias"` or `"unknown"` (an alias is by definition *not* a canonical row, so `canonical_id IS
NULL` excludes it structurally) -- `canonical`/`swap` only carry information for the `"alias"`
case, so omitting them here loses nothing this query's own result set could ever populate.

### 7.2 `get_lineage` -- split into a raw core plus a thin enveloped wrapper (Amendment 2)

**This section supersedes its own original text** (see Amendment 2 for what that original text
missed and why). `get_lineage` (lines 797-947 in full, not just the validation prefix) is
consumed two ways: (a) as the public, dispatch-facing entry point (`daemon/dispatch.py`'s
`_dispatch_get_lineage`, `memory_service/lifecycle.py`'s `_lineage_nodes`), which wants the new
envelope shape, and (b) as an internal traversal primitive reused by three other services
(`relation_service.py`'s own `analyze_lineage`, `conflict_set_service.py`'s
`_component_lifecycle_resolved`, `lineage_assembly_service.py`'s `assemble_lineage`), which
consume its *current* bare-dict shape (`"error" in result`, `result["nodes"]`, etc.) directly
and would break -- silently, via `KeyError`, not gracefully -- if that shape changed under them.
This is exactly the split the codebase already uses for `get_related_memories`/
`analyze_dependencies` (§7.3): a raw internal function untouched, a thin enveloped wrapper around
it. Apply the same pattern here instead of editing `get_lineage`'s body in place.

Step 1 -- rename the existing function, zero other change:

Before (line 797, the `def` line only -- everything from here through line 947, the entire
current body including its three `{"error": ...}` returns and its final success-dict return, is
byte-for-byte unchanged):

```python
def get_lineage(  # noqa: C901, PLR0911, PLR0912, PLR0915
```

After:

```python
def _get_lineage_raw(  # noqa: C901, PLR0911, PLR0912, PLR0915
```

Step 2 -- add a new thin `get_lineage` wrapper directly after `_get_lineage_raw` ends (i.e. where
the old function used to end, line 947), matching §7.3's `get_related_memories` shape exactly:

```python
def get_lineage(
    entity_id: str = None,
    direction: Literal["ancestors", "descendants"] = "ancestors",
    max_depth: int = 10,
    point_in_time: str = None,
    db_connection=None,
    db_path: str = None,
) -> dict:
    """Envelope-shaped wrapper around :func:`_get_lineage_raw`.

    Kept as a separate thin function, not an in-place rewrite of the raw traversal, because
    three other services (`analyze_lineage` in this same module, `conflict_set_service`,
    `lineage_assembly_service`) reuse the raw bare-dict shape directly and must not be forced
    through envelope unwrapping on every internal call.
    """
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected

    result = _get_lineage_raw(
        entity_id=entity_id,
        direction=direction,
        max_depth=max_depth,
        point_in_time=point_in_time,
        db_connection=db_connection,
        db_path=db_path,
    )
    if "error" in result:
        return rejected([envelope_error(error_codes.VALIDATION_ERROR, result["error"])])
    return envelope_ok(result)
```

This mirrors §7.3's own `get_related_memories` error-forwarding exactly (`if "error" in result:
return rejected([envelope_error(error_codes.VALIDATION_ERROR, result["error"])])` -- same
precedent, same error code, no new pattern introduced) rather than trying to preserve
per-validation-check `field` granularity: `_get_lineage_raw`'s three distinct validation
messages, plus its "Could not resolve entity" and exception-catch messages, all collapse to the
same `VALIDATION_ERROR` code with the original message text preserved, exactly as
`get_related_memories` already does for `analyze_dependencies`'s own error messages.

Step 3 -- update every production call site that imports or calls `get_lineage` expecting the
raw shape. This list was independently re-derived twice for Amendment 3 (once by grep across
`src/` and `tests/` for `get_lineage`, `import get_lineage`, and `relation_service.get_lineage`;
once via `mcp__acie__find_references` on `relation_service.py:get_lineage#function`) after
Amendment 2's own version of this list turned out to be incomplete -- ACIE's static call graph
correctly found every direct call/import but, expectedly, could not see `unittest.mock.patch`
string targets or the daemon's string-keyed dispatch table, which is exactly the category Step 6
below closes. Two kinds of edit are needed, and they are different: an **import statement** that
names `get_lineage` must have that name changed too, not just the call expression that uses it,
or Python will raise `ImportError` at collection time.

- `relation_service.py:959` (`analyze_lineage`'s own call, same file, no import statement
  involved since it's a bare same-module reference) -- `result = get_lineage(` becomes
  `result = _get_lineage_raw(`.
- `conflict_set_service.py:14` (`from saltmdb.domain.services.relation_service import
  get_lineage`) -- becomes `import _get_lineage_raw`. Lines 118 and 125
  (`_component_lifecycle_resolved`'s `ancestors_result =`/`descendants_result =` calls) --
  both `get_lineage(` become `_get_lineage_raw(`.
- `lineage_assembly_service.py:15` (`from saltmdb.domain.services.relation_service import
  get_lineage`) -- becomes `import _get_lineage_raw`. Line 82 (`assemble_lineage`'s
  `ancestors_result =` call) -- `get_lineage(` becomes `_get_lineage_raw(`.

Step 4 -- `daemon/dispatch.py`'s `_dispatch_get_lineage` and `memory_service/lifecycle.py`'s
`_lineage_nodes` keep calling `get_lineage` unchanged (it is still named `get_lineage`, just now
the enveloped wrapper) and still need the fix Amendment 1 already specified for
`_lineage_nodes`: update it (lifecycle.py:42-58) to check `result.get("status") == "rejected"` in
addition to (not instead of) `result.get("error")`, and to read the node list from
`result.get("data", {})` first (currently lines 51-55 read top-level keys directly) -- unchanged
from the original text, restated here because it now applies to the new wrapper rather than the
old single function.

Step 5 -- `src/saltmdb/viewer/routes/entity_detail.py`'s `get_lineage` method (lines 46-58) is a
**sixth production call site**, reading the pre-envelope shape directly:

Before:

```python
            get_lineage = getattr(relation_service, "get_lineage", None)
            if get_lineage is not None:
                ancestor_result = get_lineage(
                    entity_id=entity_id,
                    direction="ancestors",
                    max_depth=10,
                    db_connection=conn,
                )
                if isinstance(ancestor_result, dict) and ancestor_result.get("error"):
                    self.send_json({"error": ancestor_result["error"]}, 404)
                    return
                raw_nodes: list[tuple[dict, str]] = []
                direction_nodes = ancestor_result.get("nodes", [])
                raw_nodes.extend(
                    (node, "ancestors") for node in direction_nodes if isinstance(node, dict)
                )
```

After:

```python
            get_lineage = getattr(relation_service, "get_lineage", None)
            if get_lineage is not None:
                from saltmdb.utils.envelope import is_rejected

                ancestor_result = get_lineage(
                    entity_id=entity_id,
                    direction="ancestors",
                    max_depth=10,
                    db_connection=conn,
                )
                if isinstance(ancestor_result, dict) and is_rejected(ancestor_result):
                    message = ancestor_result.get("errors", [{}])[0].get(
                        "message", "Unknown error"
                    )
                    self.send_json({"error": message}, 404)
                    return
                raw_nodes: list[tuple[dict, str]] = []
                direction_nodes = ancestor_result.get("data", {}).get("nodes", [])
                raw_nodes.extend(
                    (node, "ancestors") for node in direction_nodes if isinstance(node, dict)
                )
```

This mirrors `_lineage_nodes`'s own Amendment-1 fix exactly (`is_rejected` in place of the old
`"error"` check, node list read from `data` first) -- same pattern, third application of it in
this file's overall change set (lifecycle.py, this method, and conceptually the new `get_lineage`
wrapper itself). The `else` branch below this (the `analyze_lineage` fallback for when
`get_lineage` doesn't exist as an attribute at all) is untouched -- `analyze_lineage`'s own return
shape never changes. Nothing downstream of `direction_nodes` in this method changes: the
node-normalization loop that builds the handler's own `nodes` output list reads generic
`node.get(...)` fields from whatever `direction_nodes` resolves to, so it produces the identical
final payload once `direction_nodes` is extracted correctly. This is **no longer an accepted
regression** -- per this project's own standing rule (a limitation is never accepted when the fix
is within our own jurisdiction), it is fixed. `src/saltmdb/viewer/**` remains out of scope for
everything else; only this one method, this one shape-extraction fix, is now licensed (§0).

Step 6 -- tests. Every one of these was found by grepping `get_lineage` and `analyze_lineage`
across the entire `tests/` directory and checking each hit's actual assertion against the new
shape, specifically because Amendment 2's own equivalent pass missed several of these:

- `tests/test_relation_service.py:26` (import list) -- `get_lineage,` becomes
  `_get_lineage_raw,`. Lines 1470, 1496, 1514, 1533 (`TestPhase3LineageGraph`'s four traversal
  tests: cycle bounding, bitemporal `valid_at` filtering, `max_depth` honoring, archived-parent
  absorption) -- each `get_lineage(` becomes `_get_lineage_raw(`. These test the raw traversal
  algorithm, not the envelope; no assertion changes needed, only the call target.
- `tests/test_lineage_assembly_service.py:16` (import) -- `from
  saltmdb.domain.services.relation_service import get_lineage, store_relation` becomes
  `import _get_lineage_raw, store_relation`. Line 290 (`real_get_lineage = get_lineage`) becomes
  `real_get_lineage = _get_lineage_raw`. Line 298's patch target
  (`"saltmdb.domain.services.lineage_assembly_service.get_lineage"`) becomes
  `"saltmdb.domain.services.lineage_assembly_service._get_lineage_raw"` -- patching a name that no
  longer exists in that module's namespace after the import rename would raise `AttributeError`
  at patch-application time, failing this test outright, not just asserting the wrong shape.
- `tests/test_conflict_set_service.py:454`'s patch target
  (`"saltmdb.domain.services.conflict_set_service.get_lineage"`) becomes
  `"saltmdb.domain.services.conflict_set_service._get_lineage_raw"` -- same `AttributeError`
  reasoning as above (this file has no separate `get_lineage` import to rename; the patch string
  is its only reference).
- `tests/test_get_memory.py`'s `test_returns_archived_entity_without_redirecting` (~line 71)
  patches `"saltmdb.domain.services.relation_service.get_lineage"` (this qualified attribute
  patch target does **not** need renaming -- `get_lineage` is still the correct name of the new
  wrapper) but its `return_value={"nodes": [{"id": successor, "depth": 1, "status": "raw"}]}` is
  the pre-envelope shape, which `_lineage_nodes`'s Amendment-1 fix (Step 4 above) will no longer
  read correctly once implemented (it reads `result.get("data", {})` first). Update the mock:
  `return_value={"status": "ok", "data": {"nodes": [{"id": successor, "depth": 1, "status":
  "raw"}]}, "warnings": []}`.
- `tests/test_mcp_tools.py:631` (`test_graph_tools_honor_depth_limits`) --
  `self.assertEqual(lineage_now["total"], 2)` reads the top-level key of `tools.get_lineage(...)`'s
  real (non-mocked) return value through the actual dispatch/service chain -- becomes
  `self.assertEqual(lineage_now["data"]["total"], 2)`.
- `tests/test_dispatch_types.py:46`'s `@patch("saltmdb.daemon.dispatch.relation_service.get_lineage",
  return_value={"nodes": []})` needs **no change** -- confirmed by reading
  `_dispatch_get_lineage`'s implementation (dispatch.py:389-401): it returns whatever
  `get_lineage(...)` gives it verbatim with no shape inspection, and this test only asserts
  `lineage.assert_called_once_with(...)` (the call arguments), never the return value's shape.
- `tests/test_viewer_routes.py`'s two shape-adjacent tests
  (`test_get_lineage_delegates_and_matches_relation_service_directly`,
  `test_get_lineage_nodes_have_depth_and_generation_depth_equal_and_expected_keys`) need **no
  assertion change** -- both assert only on the handler's own final `nodes` payload (built by the
  node-normalization loop below `direction_nodes` in Step 5's fix), which is unaffected by how
  `direction_nodes` itself is extracted. `test_get_lineage_entity_not_found_returns_error_regression`
  is unaffected for a different reason: its 404 path returns before ever calling `get_lineage` (the
  initial `entities` table lookup finds no row first).
- `tests/test_phase3_mcp_surface.py` and the rest of `test_mcp_tools.py`'s `get_lineage`
  references (schema/registration/backend-call-recording checks) do not inspect result shape and
  need no change -- confirmed by reading each one.

**No cascading call site beyond this list**: re-verified via `grep -rn "import get_lineage\|
get_lineage,\|, get_lineage" src/ tests/` and `grep -rn "relation_service\.get_lineage\|
relation_service, .get_lineage.\|\"get_lineage\"" src/ tests/` (both run fresh during Amendment
3's drafting) that every remaining reference not enumerated above is either the `"get_lineage"`
MCP/dispatch-table tool-name string (unrelated to the function rename) or one of the sites already
covered.

### 7.3 `get_related_memories` -- drop the duplicate key (Q9)

Before (lines 990-1026):

```python
def get_related_memories(
    entity_id: str = None,
    max_depth: int = 5,
    point_in_time: str = None,
    direction: Literal["outbound", "inbound", "both"] = "both",
    db_connection=None,
    db_path: str = None,
    include_inspect: bool = False,
) -> dict:
    """..."""
    result = analyze_dependencies(
        root_entity_id=entity_id,
        max_depth=max_depth,
        point_in_time=point_in_time,
        direction=direction,
        include_inspect=include_inspect,
        db_connection=db_connection,
        db_path=db_path,
    )
    if "error" in result:
        return result
    # Keep the historical keys while exposing the terminology of the new tool. This is
    # useful to in-process callers during the MCP/daemon registration migration.
    return {
        **result,
        "entity_id": result["root"]["id"],
        "related_memories": result["dependencies"],
        "total_related_found": result["total_dependencies_found"],
        "max_depth": max_depth,
    }
```

After:

```python
def get_related_memories(
    entity_id: str = None,
    max_depth: int = 5,
    point_in_time: str = None,
    direction: Literal["outbound", "inbound", "both"] = "both",
    db_connection=None,
    db_path: str = None,
    include_inspect: bool = False,
) -> dict:
    """..."""
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected

    result = analyze_dependencies(
        root_entity_id=entity_id,
        max_depth=max_depth,
        point_in_time=point_in_time,
        direction=direction,
        include_inspect=include_inspect,
        db_connection=db_connection,
        db_path=db_path,
    )
    if "error" in result:
        return rejected([envelope_error(error_codes.VALIDATION_ERROR, result["error"])])
    payload = {k: v for k, v in result.items() if k != "dependencies"}
    payload["entity_id"] = result["root"]["id"]
    payload["related_memories"] = result["dependencies"]
    payload["total_related_found"] = result["total_dependencies_found"]
    payload["max_depth"] = max_depth
    return envelope_ok(payload)
```

The migration-shim comment ("Keep the historical keys...") is removed along with the key it was
justifying -- `dependencies` is gone from the output entirely; only `related_memories` remains,
carrying the identical node list (root included, at index 0, exactly as `dependencies` used to).
`root` (the separate, single-root-info key `analyze_dependencies` also returns) is unchanged and
still present via the dict-comprehension's pass-through -- this spec does not touch it, per Q9's
explicit minimal scope (no direction/predicate labeling added).

**No cascading call site**: `grep -rn "\[.dependencies.\]\|\.get(.dependencies.)" src/ tests/`
(run this exact command during implementation) to confirm nothing outside this function itself
reads the `dependencies` key from `get_related_memories`'s own output specifically (as opposed to
`analyze_dependencies`'s own output, which is unchanged and keeps its `dependencies` key --
this spec does not touch `analyze_dependencies` itself, only this thin wrapper around it).

### 7.4 `consolidate_memories`/`_consolidation_rejected` -- reuse the real envelope (decision 10)

Before (lines 1137-1143):

```python
def _consolidation_rejected(code: str, message: str) -> dict:
    """Build the Phase 4 mutation envelope for a validation rejection."""
    return {
        "status": "rejected",
        "errors": [{"code": code, "message": message}],
        "warnings": [],
    }
```

After:

```python
def _consolidation_rejected(code: str, message: str) -> dict:
    """Build the Phase 4 mutation envelope for a validation rejection."""
    from saltmdb.utils.envelope import error as envelope_error, rejected

    return rejected([envelope_error(code, message)])
```

Before (lines 1642-1656, the success return):

```python
        return {
            "status": "ok",
            "data": {
                "entity_id": consolidated_id,
                "message": f"Successfully committed consolidated memory with ID: {consolidated_id}",
                "orphaned_relations": orphaned_edges,
                "orphaned_edge_worklist": orphaned_edges,
                "orphaned_edges": orphaned_edges,
                "worklist_guidance": (
                    "Re-declaring these relations through manage_relation is optional; "
                    "skipping them leaves a correct historical graph."
                ),
            },
            "warnings": [],
        }
```

After:

```python
        from saltmdb.utils.envelope import ok as envelope_ok

        return envelope_ok(
            {
                "entity_id": consolidated_id,
                "message": f"Successfully committed consolidated memory with ID: {consolidated_id}",
                "orphaned_relations": orphaned_edges,
                "orphaned_edge_worklist": orphaned_edges,
                "orphaned_edges": orphaned_edges,
                "worklist_guidance": (
                    "Re-declaring these relations through manage_relation is optional; "
                    "skipping them leaves a correct historical graph."
                ),
            }
        )
```

Both changes produce byte-identical output to today's hand-rolled dicts for every existing
caller and test -- this is a pure implementation-reuse swap with zero observable behavior change,
the lowest-risk item in this entire spec. `_consolidation_rejected`'s own call sites (every
validation check inside `consolidate_memories`, unchanged) need no edits -- they already call
`_consolidation_rejected(code, message)` and will keep receiving the identical shape back.

### 7.5 `store_relation` -- envelope conversion (decision 2, the biggest single function here)

Before (representative excerpt, full function is lines 134-463): returns bare strings throughout
-- `"Error: source_id, target_id, and predicate are mandatory parameters."` (line 173), and
(per the earlier audit, not re-quoted from this pass's own reads but confirmed present at the
function's success return further down) a success string of the shape `"Relation successfully
stored: '<predicate>' (ID: <relation_id>)"`, plus various other `"Error: ..."`-prefixed and
`"Relation already exists..."`-prefixed strings for other branches within the function (the
governance-gate rejections, the core-elaborates-on rejection, the idempotent-duplicate case).

After: apply the same `rejected()`/`envelope_ok()` pattern shown in full for `archive_memory`
(§6.3) to every one of this function's `return "..."` statements -- `VALIDATION_ERROR` for the
mandatory-parameters check, reuse-or-introduce a specific code per distinct rejection reason
(the governance gate's `REJECT_LOW_RELATION_SIMILARITY`/`REJECT_CONTRADICTORY_PREDICATE`/
`REJECT_CORE_ELABORATES_ON` names, already used as prose today per this function's own
docstring, become the `code` values verbatim -- they are already specific and already documented
in the docstring, just never attached to a structured field), `ALREADY_DONE` for the idempotent
duplicate-edge no-op (wrapped in `envelope_ok`, not `rejected` -- same reasoning as
`archive_memory`'s already-archived case), and `envelope_ok({"relation_id": ..., "message":
...})` for the real success path. **This function's exact remaining branch structure was not
re-read line-by-line in this pass past line 173** (a ~330-line function) -- during
implementation, read `store_relation` in full first and enumerate every one of its `return`
statements before touching any of them, applying this same substitution pattern to each; do not
guess at a branch's exact current message text from this spec, copy it verbatim from the source
and keep the same wording, only change the wrapping shape.

**Cascading call sites, exhaustive** (found via `grep -rn "store_relation(" src/`):

1. `relation_service.py`'s own `bulk_store_relations` (§7.7 below).
2. `core_governance_service.py:311-320` (`reconcile_detail_relations`):

   Before:
   ```python
           res = store_relation(
               source_id=detail_id, target_id=core_id, predicate="elaborates_on",
               owner_id=owner_id, db_connection=conn, _in_transaction=True,
               _allow_core_elaborates_on=True,
           )
           if isinstance(res, str) and res.startswith("Error"):
               raise RuntimeError(f"Failed to create detail relation for {detail_id}: {res}")
   ```
   After:
   ```python
           res = store_relation(
               source_id=detail_id, target_id=core_id, predicate="elaborates_on",
               owner_id=owner_id, db_connection=conn, _in_transaction=True,
               _allow_core_elaborates_on=True,
           )
           if envelope.is_rejected(res):
               raise RuntimeError(
                   f"Failed to create detail relation for {detail_id}: {res['errors'][0]['message']}"
               )
   ```
   (`import saltmdb.utils.envelope as envelope` at this file's top, alongside its existing
   imports.)

### 7.6 `invalidate_relation` -- envelope conversion

Before (representative excerpt, lines 464+): `"Error: source_id, target_id, and predicate are
mandatory parameters."` (line 480, identical wording to `store_relation`'s own check), plus (per
the earlier audit) a success string and an "already invalidated" string further into the
function.

After: identical substitution pattern to §7.5 -- `VALIDATION_ERROR` for the mandatory-parameters
check, `ALREADY_DONE` for the already-invalidated no-op, `envelope_ok(...)` for the real success
path. Read the function's full remaining body during implementation before editing, same caveat
as §7.5.

**Cascading call sites**: `bulk_store_relations` (§7.7). `core_governance_service.py`'s
`reconcile_detail_relations` also calls `invalidate_relation` (after line 327) but does **not**
currently check its return value at all (the comment there reads "best-effort... not fatal
here") -- no change needed at that specific call site, since ignoring a dict is exactly as safe
as ignoring a string.

### 7.7 `bulk_store_relations` -- envelope conversion, updating its own internal string-sniffing

Before (lines 1835-1934, the string-sniffing portions):

```python
    if not relations or not isinstance(relations, list):
        return [{"status": "error", "error": "relations must be a non-empty array of objects"}]
    ...
                if item_invalidate:
                    res = invalidate_relation(...)
                    if res.startswith("Error"):
                        raise RuntimeError(f"Bulk relation store aborted (all-or-nothing): {res}")
                    status = (
                        "duplicate" if res.startswith("Relation already invalidated") else "success"
                    )
                    results.append({..., "result": res})
                    continue
                res = store_relation(...)
                if res.startswith("Error"):
                    raise RuntimeError(f"Bulk relation store aborted (all-or-nothing): {res}")
                status = "duplicate" if res.startswith("Relation already exists") else "success"
                results.append({..., "result": res})
    ...
    except Exception as e:
        return [{"status": "error", "error": str(e)}]
```

After (pattern -- apply to both the invalidate and store branches identically):

```python
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected
    import saltmdb.utils.envelope as envelope

    if not relations or not isinstance(relations, list):
        return rejected(
            [envelope_error(error_codes.VALIDATION_ERROR, "relations must be a non-empty array of objects", "relations")]
        )
    ...
                if item_invalidate:
                    res = invalidate_relation(...)
                    if envelope.is_rejected(res):
                        raise RuntimeError(
                            f"Bulk relation store aborted (all-or-nothing): {res['errors'][0]['message']}"
                        )
                    is_dup = any(w.get("code") == error_codes.ALREADY_DONE for w in res.get("warnings", []))
                    results.append({
                        "status": "duplicate" if is_dup else "success",
                        "source": src, "target": tgt, "predicate": pred,
                        "action": "invalidate", "result": res["data"],
                    })
                    continue
                res = store_relation(...)
                if envelope.is_rejected(res):
                    raise RuntimeError(
                        f"Bulk relation store aborted (all-or-nothing): {res['errors'][0]['message']}"
                    )
                is_dup = any(w.get("code") == error_codes.ALREADY_DONE for w in res.get("warnings", []))
                results.append({
                    "status": "duplicate" if is_dup else "success",
                    "source": src, "target": tgt, "predicate": pred,
                    "action": "store", "result": res["data"],
                })
    ...
    except Exception as e:
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
```

`bulk_store_relations`'s own **return type stays a bare `list`** (its per-item results list,
`results`) -- this function's docstring and every caller already treat "a list of per-item result
dicts" as its contract, and that contract is not itself broken or inconsistent (each item is
already a well-formed dict); only the *inputs* it reads from `store_relation`/`invalidate_relation`
need updating for their shape change, plus its own top-level `relations` validation and
whole-batch exception fallback (both fixed above) which previously had no error code at all.

## 8. `src/saltmdb/domain/services/memory_service/write.py`

### 8.1 `store_memory`'s exception fallback

Before (lines ~917-921):

```python
    except Exception as e:
        logger.error("Error storing knowledge: %s", e)
        return f"Error storing knowledge: {e}"
```

After:

```python
    except Exception as e:
        from saltmdb.utils import error_codes

        logger.error("Error storing knowledge: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
```

(`envelope_error`/`rejected` -- confirm this file's existing import alias for
`saltmdb.utils.envelope`'s `error`/`rejected` functions before writing this; `envelope_ok` is
already used by this function's happy path per the audit, so an alias already exists somewhere
in this file's imports -- reuse it rather than introducing a second alias for the same names.)

### 8.2 `_assemble_memory_record` (`memory_service/lifecycle.py`) -- native JSON types (decision 9)

Before (lines 784, 787, 792):

```python
        "parent_ids": row[10],
```
```python
        "metadata": row[13],
```
```python
        "quality_flags": row[18],
```

After:

```python
        "parent_ids": json.loads(row[10]) if row[10] else [],
```
```python
        "metadata": json.loads(row[13]) if row[13] else {},
```
```python
        "quality_flags": json.loads(row[18]) if row[18] else [],
```

(`json` is already imported at this file's top, line 7.) This changes `get_memory`/
`inspect_memory`/`get_related_memories(include_inspect=True)`'s output for these three fields
from a JSON-encoded string to the native parsed value -- matching how `tags` (assembled two
lines below via `tag_ops.list_entity_tags`) already returns a native list, not a string. The
underlying SQLite storage format is unchanged (these columns stay JSON-text in the `entities`
table; only this read boundary is fixed).

**Cascading test updates, exhaustive** (found via `grep -n 'json.loads(fetched\["data"\]\["metadata"\])' tests/test_mcp_tools.py`):
`tests/test_mcp_tools.py` lines 1359, 1372, 1453 each currently do
`json.loads(fetched["data"]["metadata"])`, asserting against the *parsed* value -- since
`fetched["data"]["metadata"]` is now already the parsed native dict, remove the `json.loads(...)`
wrapper at each of these three call sites, comparing `fetched["data"]["metadata"]` directly. No
other call site in the tree re-parses these three fields from a `get_memory`/`inspect_memory`
result (confirmed: `write.py:749`'s own `json.loads(frozen_current["metadata"])` and both
`viewer/routes/*.py` call sites read directly from a raw SQL row dict, never from this function's
output, and are untouched by this change).

## 9. `src/saltmdb/domain/services/memory_service/tags.py`

### 9.1 `search_tags` -- envelope conversion

Before (lines 194-232, full function):

```python
def search_tags(
    domain: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> list:
    """..."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        if domain:
            cursor = conn.execute(
                "SELECT id, name FROM tags WHERE canonical_id IS NULL AND name LIKE ? LIMIT ?",
                (f"%{domain}%", limit),
            )
        else:
            cursor = conn.execute(
                "SELECT id, name FROM tags WHERE canonical_id IS NULL LIMIT ?", (limit,)
            )
        rows = cursor.fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except Exception as e:
        logger.error("Error fetching canonical tags: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
```

After:

```python
def search_tags(
    domain: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> dict:
    """..."""
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        if domain:
            cursor = conn.execute(
                "SELECT id, name FROM tags WHERE canonical_id IS NULL AND name LIKE ? LIMIT ?",
                (f"%{domain}%", limit),
            )
        else:
            cursor = conn.execute(
                "SELECT id, name FROM tags WHERE canonical_id IS NULL LIMIT ?", (limit,)
            )
        rows = cursor.fetchall()
        return envelope_ok([{"id": r[0], "name": r[1]} for r in rows])
    except Exception as e:
        logger.error("Error fetching canonical tags: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
    finally:
        if should_close:
            close_connection(conn)
```

Empty results now come back as `envelope_ok([])` -- an explicit, unambiguous empty-success shape
(the caller can distinguish "zero tags matched" from a rejection by checking `status`), which is
also this spec's fix for the "no explicit empty-list signal" half of the pagination finding from
the original audit, independent of the deferred `has_more` piece (§1's scope note).

## 10. `src/saltmdb/domain/services/librarian_service.py`

### 10.1 `merge_tags` -- envelope conversion

Before (lines 205-258, full function): `return f"Error: keep_tag '{keep_tag}' does not exist in
the tags table."` (line 222) and `return f"Merged {len(merged)} tag(s) into canonical tag
'{keep_tag}': {merged}. Skipped: {skipped}"` (line 255) -- no exception handler at all around the
write (an unhandled exception here already propagates as a real Python exception today; this
spec does not add a new `try/except` around the write itself, since doing so would be a
behavior change beyond "make the existing two return paths consistent" -- see §16).

After:

```python
def merge_tags(
    keep_tag: str, tags_to_merge: list, conn: sqlite3.Connection = None, db_path: str = None
) -> dict:
    """..."""
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected

    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        canonical_id = _resolve_tag_id(conn, keep_tag)
        if not canonical_id:
            return rejected(
                [envelope_error(error_codes.NOT_FOUND, f"keep_tag '{keep_tag}' does not exist in the tags table.", "keep_tag")]
            )

        merged: list[Any] = []
        skipped: list[Any] = []

        def _write(c):
            ... # unchanged body

        write_transaction_retrying(conn, _write)

        return envelope_ok({"merged": merged, "skipped": skipped, "canonical_tag": keep_tag})
    finally:
        if should_close:
            close_connection(conn)
```

**No cascading call site**: `grep -rn "librarian_service.merge_tags(\|merge_tags(" src/` (run
during implementation) to confirm the only callers are `daemon/dispatch.py`'s
`"merge_tags": lambda **kw: librarian_service.merge_tags(**kw)` one-liner and
`mcp/tools.py`'s own `merge_tags` tool wrapper -- neither inspects the return value's shape
beyond passing it straight through, so no cascading update needed here (unlike `store_relation`/
`archive_memory`/`log_event`, which have real internal consumers of their old string shape).

## 11. `src/saltmdb/domain/services/event_service.py`

### 11.1 `log_event` -- envelope conversion

Before (lines 79-82):

```python
        return f"Event logged successfully with ID: {event_id}"
    except Exception as e:
        logger.error("Error logging event: %s", e)
        return f"Error logging event: {e}"
```

After:

```python
        return envelope_ok({"id": event_id, "message": f"Event logged successfully with ID: {event_id}"})
    except Exception as e:
        logger.error("Error logging event: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
```

(Add `from saltmdb.utils import error_codes` and
`from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected` to this
file's import block, currently lines 1-7.)

**Cascading call sites, exhaustive** (found via `grep -n '\.startswith("Error")' src/saltmdb/domain/services/relation_service.py`
matched against this function specifically -- both are `log_event(..., _in_transaction=True)`
calls, confirmed by reading the surrounding code, not `store_relation`/`invalidate_relation`):

1. `relation_service.py:~397` (inside `store_relation`, logging a `relation_gate_override`
   audit event):
   Before:
   ```python
                   if audit_result.startswith("Error"):
                       raise RuntimeError(
                           f"Failed to record relation gate override audit event: {audit_result}"
                       )
   ```
   After:
   ```python
                   if envelope.is_rejected(audit_result):
                       raise RuntimeError(
                           f"Failed to record relation gate override audit event: {audit_result['errors'][0]['message']}"
                       )
   ```
2. `relation_service.py:~1494` (inside `consolidate_memories`, logging a consolidation-override
   audit event) -- identical substitution.

(`import saltmdb.utils.envelope as envelope` once at this file's top covers both call sites, and
is also needed by §7.5/§7.6/§7.7's own cascading updates in this same file -- add it once.)

## 12. `src/saltmdb/domain/services/core_governance_service.py`

### 12.1 `review_core_memory` -- envelope conversion

Before (lines 1025-1141, full function): returns bare strings throughout, both error (`"Error:
outcome must be one of 'retain', 'demote', 'archive'."`, `"Error: owner_id is mandatory."`, etc.)
and success/no-op (`f"Memory '{resolved_id}' is already non-core (no-op)."`,
`f"Memory '{resolved_id}' demoted from core to a normal memory."`, etc., all assembled into
`result_holder["msg"]` and returned as the function's final value).

After: apply the same pattern as §6.3/§6.4 -- `VALIDATION_ERROR` for the four early
validation checks (outcome enum, owner_id mandatory, core_review_after misuse, rationale
validation failure), `NOT_FOUND` for the not-found case, `ALREADY_DONE` (wrapped in
`envelope_ok` with a warning, per §2's documented semantics) for both no-op cases ("already
non-core", "already archived"), and `envelope_ok({"id": resolved_id, "message": ...})` for the
two real-mutation success cases (demoted, archived). Read the function's full body during
implementation before editing (it was read only at its signature/opening validation in this
pass) -- apply the substitution to every one of its return points using the same reasoning
already fully worked in §6.3.

### 12.2 Capacity-gate rejection -- reconcile into the same taxonomy

Before (lines 682-699, the capacity-gate rejection dict, reached from `store_memory`/
`consolidate_memories`'s core-capacity check):

```python
        "status": "REJECTED",
        "error_code": "CORE_CAPACITY_EXCEEDED",
```

(plus its surrounding `violated_dimensions`/`limits`/etc. fields, unchanged.)

After:

```python
        "status": "rejected",
        "errors": [{"code": "CORE_CAPACITY_EXCEEDED", "message": <the existing message text>}],
```

Restructure this dict's top two keys from the ad hoc `{"status": "REJECTED", "error_code": ...}`
shape into the standard `envelope.rejected()` shape (`{"status": "rejected", "errors": [...]}`),
keeping every other field this dict currently carries (`violated_dimensions`, `limits`, etc.) as
additional top-level keys alongside `status`/`errors` -- `envelope.rejected()`'s own signature
does not accept arbitrary extra top-level keys, so construct this one by hand
(`{"status": "rejected", "errors": [envelope_error("CORE_CAPACITY_EXCEEDED", message)],
"warnings": [], **existing_extra_fields}`) rather than calling `rejected()` directly, matching
the same "hand-build to the same shape when extra fields are needed" precedent
`_replacement_operation`'s own `AMBIGUOUS_ID_PREFIX` branch already uses (lifecycle.py:310-318,
which adds a `candidates` key onto an `envelope_error(...)` dict by hand before wrapping it).
`CORE_CAPACITY_EXCEEDED` itself is **not** renamed -- it is already specific and already used
this way; only the enclosing envelope's `status` casing (`"REJECTED"` → `"rejected"`) and shape
(`error_code` singular → `errors` list) change, to match every other tool's rejection shape.

**Read this section's exact current field names and surrounding code in full during
implementation** before editing -- this pass read only the two lines identified by the earlier
audit (`core_governance_service.py:682-699`) and did not re-verify the complete dict literal or
every one of its current callers' field-access patterns; grep
`grep -rn '"error_code"\]\|\["error_code"\]\|status.*REJECTED\|REJECTED.*status' src/ tests/`
before changing this shape, and update every reader of the old `error_code`/`REJECTED` fields
(most likely `mcp/tools.py`'s `store_memory`/`consolidate_memories` wrappers and their own
tests) to read the new `errors[0]["code"]`/`"rejected"` shape instead.

**Amendment 1 -- five stale documentation references to the old shape, found by grepping the
above pattern (plus `*.md`, which the original pattern omitted) against the real tree:**

1. `src/saltmdb/mcp/tools.py:383` (`store_memory`'s own docstring), before:
   ```
   bootstrap digest. A capacity failure returns `status: "REJECTED"` with `error_code:
   "CORE_CAPACITY_EXCEEDED"`, a balanced inventory of every active core (no full content), and
   zero side effects
   ```
   after:
   ```
   bootstrap digest. A capacity failure returns `status: "rejected"` with `errors[0].code ==
   "CORE_CAPACITY_EXCEEDED"`, a balanced inventory of every active core (no full content), and
   zero side effects
   ```
2. `src/saltmdb/domain/services/memory_service/write.py:610` (`store_memory`'s own docstring),
   before: `` a capacity failure returns a `status: "REJECTED"` dict with zero side effects ``,
   after: `` a capacity failure returns a `status: "rejected"` envelope (`errors[0].code ==
   "CORE_CAPACITY_EXCEEDED"`) with zero side effects ``.
3. `README.md:193` (embedded in the `store_memory` table row), before: `` a capacity failure
   returns `status: "REJECTED"` with zero side effects. `` -- this clause was already describing
   a *deliberate exception* to the same row's own preceding sentence ("Responses use a uniform
   envelope: `{"status": "ok"/"rejected", ...}`"); after this spec it is no longer an exception,
   so simplify rather than just re-case: delete the whole trailing clause from `; a capacity
   failure...` onward, since the immediately preceding sentence in the same row now already
   covers this case correctly with no exception to carve out.
4. `README.md:234` (the dedicated "Capacity failures are side-effect-free" paragraph), before:
   `` A write that would exceed any limit returns `status: "REJECTED"`, `error_code:
   "CORE_CAPACITY_EXCEEDED"`, the violated dimensions, ... ``, after: `` A write that would
   exceed any limit returns `status: "rejected"` with an `errors[0].code ==
   "CORE_CAPACITY_EXCEEDED"` entry, the violated dimensions, ... ``.
5. `AGENT_GUIDE.md:225` (embedded in the `store_memory` bullet's "Core-memory governance"
   clause) -- **not found by the coordinator's own spot-check, confirmed independently by
   re-running the grep against `*.md` during this amendment**, before: `` a capacity failure
   returns `status: "REJECTED"` (`error_code: "CORE_CAPACITY_EXCEEDED"`) with a balanced
   inventory and zero side effects ``, after: `` a capacity failure returns `status: "rejected"`
   (`errors[0].code == "CORE_CAPACITY_EXCEEDED"`) with a balanced inventory and zero side
   effects ``.

Re-run `grep -rn '"error_code"\]\|\["error_code"\]\|status.*REJECTED\|REJECTED.*status'
src/ tests/ README.md AGENT_GUIDE.md` immediately before considering this section done, to
confirm no sixth reference exists beyond the five named above.

## 13. `src/saltmdb/mcp/tools.py` -- batch item schemas (decision 8)

`manage_relation`'s `relations: list | None` (line 650) and `consolidate_memories`'s
`consolidations: list | None` (line 779) are the two tools whose batch parameter is currently
typed `Array<unknown>` in the exposed MCP schema (a bare, unparameterized `list` produces no
`items` type). Both are batches of structured dicts with a known, already-implemented shape
(`bulk_store_relations`'s per-item field reads, relation_service.py:1873-1878; the analogous
per-item reads inside `_dispatch_consolidate_memories`'s `consolidations` branch,
`dispatch.py:305-319`, and `relation_service.py`'s `bulk_commit_consolidation`).

Add, near the top of `mcp/tools.py` (after its existing imports, before the first `@mcp.tool()`):

```python
from typing import TypedDict


class RelationBatchItem(TypedDict, total=False):
    source_id: str
    target_id: str
    predicate: str
    valid_at: str | None
    invalidate: bool
    invalid_at: str | None
    override_justification: str | None


class ConsolidationBatchItem(TypedDict, total=False):
    parent_ids: list[str]
    title: str
    content: str
    is_core: bool | None
    tags: list[str] | None
    scope: Literal["private", "shared"] | None
    override_justification: str | None
```

Then change `manage_relation`'s `relations: list | None = None` (line 650) to
`relations: list[RelationBatchItem] | None = None`, and `consolidate_memories`'s
`consolidations: list | None = None` (line 779) to
`consolidations: list[ConsolidationBatchItem] | None = None`. `total=False` on both `TypedDict`s
means every field stays optional at the type level (matching today's actual runtime leniency --
`dispatch.py`'s own per-item `.get(...)` reads already tolerate a missing key) while still
giving FastMCP's schema generation a concrete `items: {type: object, properties: {...}}` shape
instead of `items: {}` -- this is a pure type-annotation addition, no runtime behavior changes at
all (`TypedDict` has no runtime enforcement of its own; `dispatch.py`'s existing `.get(...)`-based
reads are completely unaffected and untouched).

`store_memory`'s/`consolidate_memories`'s own `detail_memory_ids: list | None` and
`merge_tags`'s `tags_to_merge: list | str | None` remain untouched by this section -- those are
lists of plain strings, not structured batch items, and are addressed instead by decision 9's
native-JSON-type pass (§8.2) if and when their own schema annotations are tightened; no
`TypedDict` is needed for a list of strings (`list[str]` alone is already sufficient and is not
currently missing on `store_memory`'s `tags` parameter, which is already typed `list[str]`).

## 14. `tests/test_mcp_tools.py` and other existing tests -- fixing shape-dependent assertions

Beyond the three `json.loads(fetched["data"]["metadata"])` call sites already named in §8.2,
**every existing test that currently asserts an exact string return value** for any of the
functions changed above (`archive_memory`, `update_memory_metadata`, `store_relation`,
`invalidate_relation`, `bulk_store_relations`, `list_predicates`, `get_lineage`,
`get_related_memories`, `consolidate_memories`, `search_tags`, `merge_tags`, `log_event`,
`review_core_memory`, `store_memory`'s exception-fallback path, `revise_memory`/
`supersede_memory`'s tags-required-error path) needs its assertion updated from a string/bare-list
check to an envelope-shape check (`result["status"] == "ok"`/`"rejected"`,
`result["data"][...]`/`result["errors"][0]["code"]`). **This spec does not enumerate every such
test by name** -- that enumeration is the acceptance run's own job (§17's first command finds
every test that currently fails against the new shape); fix each one as it surfaces, applying the
same shape-check substitution throughout, per this workspace's TDD practice of red-then-green
against the real, current test suite rather than a pre-guessed list.

## 15. `src/saltmdb/domain/services/telemetry_service.py` -- stale docstring only

`classify_result`'s docstring (lines 29-35) says: "Tool return shapes are heterogeneous
pre-envelope (Phase 1)... This is deliberately loose and MUST be revisited once §4.2's envelope
is actually wired into tool responses (Phase 2+)". After this spec, that condition is met for all
19 tools. Update the docstring to remove the now-stale "MUST be revisited" framing (state simply
that all 19 tools return the envelope shape, and this function's fallback branches for a bare
string/other dict shapes are retained defensively for any future non-conforming caller, not
because one is expected today). **The function's actual logic is not required to change** --
its existing `status == "rejected"` branch (lines 43-46) already reads `errors[0]["code"]`
correctly for every tool converted by this spec, and its other branches (bare-string,
uppercase-`REJECTED`) become dead code paths for the 19 built-in tools but are harmless to leave
in place as defensive fallbacks; removing them is an unrelated cleanup this spec does not
require and should not attempt (Coding Standards rule 6).

## 16. Out of scope

- **`search_memory`/`search_tags` pagination `has_more` signaling** (originally decision 7):
  deferred to a follow-up spec. Found during this spec's own pre-lock investigation (§1) that
  fixing this properly requires changing `memory_service.search_memory`'s return type from a
  bare `list[dict]` to a wrapping dict, cascading into every internal caller of that function
  across the codebase (including `retrieve_context_service.py`'s Milestone A/D pipeline) -- an
  unenumerated, separately-sizable blast radius, unlike everything else in this spec. §9's
  `search_tags` fix (wrapping its already-list-shaped success/error in `envelope_ok`/`rejected`)
  is a smaller, contained, in-scope piece of the same original finding and is NOT deferred; only
  the `has_more`/cursor-dedup piece is.
- **Any tool-description/docstring text rewrite** beyond what's strictly needed to keep a
  docstring from contradicting a behavior/schema change made here (e.g. `revise_memory`'s
  docstring already says `context_id`/`scope`/`memory_type` are "inherited when omitted" and
  needs one clause added for `tags` joining that list; it does not get a full rewrite). The full
  rewrite is Tool Description Rewrite, the second spec, written after this one merges.
- **Relation-direction/predicate labeling on `get_related_memories`'s nodes** -- explicitly
  rejected in the locked grilling record (Q9); this spec's §7.3 is the minimal dedup fix only.
- **Any change to the graph-aware-context-retrieval feature** (Milestones A-E, `retrieve_context`,
  already merged to `develop`) beyond `_dispatch_retrieve_context`'s own validation-mechanism
  change (§4.5, which changes *how* its existing "query is required" check reports failure, not
  *what* it checks) and this file's own participation in the general dispatch.py pattern (§4.2).
  `retrieve_context_service.py`'s own internal algorithm, `assemble_retrieve_context`, is
  untouched.
- **The trace-layer/`search_trace` tool concept** from Codex's 2026-09-15 session -- discussion
  only, never approved, not part of this effort.
- **The consolidation-lineage discrepancy** documented in SALTMDB memory `841b2cfe` (`get_lineage`
  returning empty ancestors/descendants after `consolidate_memories`+`archive_memory` despite
  `parent_ids` still showing on the consolidated record) -- a separate, unresolved bug with an
  unexamined root cause. Not fixed here; if it resurfaces during this spec's own test-writing
  pass (e.g. a new `get_lineage` envelope test happens to exercise this path), leave it exactly
  as it currently behaves and do not attempt a fix as a drive-by.
- **`daemon/protocol.py`'s own wire-level error code enum** (`AUTH_FAILED`, `UNKNOWN_TOOL`,
  `MALFORMED_REQUEST`, `INTERNAL_ERROR`, `DAEMON_SHUTTING_DOWN`, `CALLER_SESSION_INVALID`) --
  unchanged. These classify transport/RPC-level failures before a request reaches a tool's own
  logic and are a distinct concern from `error_codes.py`'s domain-level taxonomy (§2); this spec
  does not merge, rename, or otherwise touch them.
- **Any already-envelope-adopted tool's existing, specific error codes** (`MISSING_TITLE`,
  `INVALID_TAGS`, `UNKNOWN_ENTITY_ID`, `AMBIGUOUS_ID_PREFIX`, `INACTIVE_TARGET`, `TARGET_CHANGED`,
  `INVALID_MEMORY`, `MEMORY_QUALITY_REJECTED`, `LIFECYCLE_WRITE_FAILED`, `RESERVED_PREDICATE`,
  `LEGACY_READONLY_PREDICATE`, `NONCANONICAL_PREDICATE`, `UNKNOWN_PREDICATE`,
  `CORE_CAPACITY_EXCEEDED` itself, `IDENTITY_IN_YAML_FRONT_MATTER`) -- kept exactly as-is; only
  their enclosing envelope shape changes where it was previously non-standard (§12.2).
- **`analyze_dependencies`'s own return shape** (the function `get_related_memories` wraps) --
  unchanged; only `get_related_memories`'s own thin wrapper (§7.3) is touched.
- **`run_librarian_now`/`trigger_librarian`/`merge_tags_heuristics`** and any other
  daemon-internal-only function not reachable through `DISPATCH_TABLE` -- not part of the 19
  exposed MCP tools' contract, out of scope.
- **Removing `telemetry_service.classify_result`'s now-dead-for-builtin-tools fallback branches**
  -- defensive code left in place per §15, not cleaned up as a drive-by.
- **`daemon/protocol.py` itself** -- no wire-protocol change; this entire spec's fix is fully
  contained within the dispatch/domain-service layers and requires no change to framing,
  auth, or RPC method classification (`READ_TOOLS`/`WRITE_TOOLS`/`MUTATING_TOOLS` all unchanged).

## 17. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
Must exit 0. Baseline confirmed clean on `develop` @ `8dd9102` immediately before this spec's own
drafting: **1726 passed, 18 subtests passed, 0 failures** (2026-09-19). Every test broken by a
shape change in this spec (§14) must be fixed to assert the new envelope shape, not skipped,
xfailed, or deleted.

```bash
rg -n 'raise ValueError' src/saltmdb/daemon/dispatch.py
```
Must show **zero** matches remaining in the `_required_*`/`_optional_*` helpers or any
`_dispatch_*` function (a real, non-validation `RuntimeError`/other exception type elsewhere in
this file, if any exists outside these helpers, is not in scope and is not what this check is
for -- confirm any remaining match is not one of the 12 validation helpers or a
`_dispatch_*` function before treating a nonzero result as a real failure).

```bash
rg -n '"status": "ok"|status": "rejected"|envelope_ok\(|envelope\.ok\(|import.*envelope' src/saltmdb/domain/services/relation_service.py src/saltmdb/domain/services/memory_service/tags.py src/saltmdb/domain/services/librarian_service.py src/saltmdb/domain/services/event_service.py src/saltmdb/domain/services/memory_service/lifecycle.py
```
Must show the envelope module imported and used in every file this spec names in §6-§11 --
reconcile every file this spec's own mechanical sections named against this grep's actual
output before considering this spec's implementation complete (pre-lock gate step 3's own
discipline, applied here as OMP's own final self-check, not just this spec-writing pass's).

```bash
rg -n 'get_lineage' src/saltmdb/domain/services/relation_service.py src/saltmdb/domain/services/conflict_set_service.py src/saltmdb/domain/services/lineage_assembly_service.py
```
Every match in `conflict_set_service.py` and `lineage_assembly_service.py` must read
`_get_lineage_raw` (both their import statements and their call sites -- Amendment 3 found the
import-statement rename was the part Amendment 2's own text omitted). In `relation_service.py`,
matches must be exactly: the new `def get_lineage(` wrapper, its own internal call to
`_get_lineage_raw(...)`, `def _get_lineage_raw(` itself, and `analyze_lineage`'s call
(`_get_lineage_raw(`) -- no remaining bare `get_lineage(` call inside `analyze_lineage`.
`daemon/dispatch.py` and `memory_service/lifecycle.py` are deliberately excluded from this grep
-- they correctly keep calling `get_lineage` (the wrapper).

```bash
rg -n 'get_lineage' tests/test_relation_service.py tests/test_lineage_assembly_service.py tests/test_conflict_set_service.py
```
`test_relation_service.py`: the import line and all four `TestPhase3LineageGraph` call sites
(§7.2 Step 6) must read `_get_lineage_raw`, not `get_lineage`. `test_lineage_assembly_service.py`:
its import line, `real_get_lineage` assignment, and patch-target string must all read
`_get_lineage_raw`. `test_conflict_set_service.py`: its patch-target string must read
`_get_lineage_raw`.

```bash
rg -n 'ancestor_result\.get\("nodes"|ancestor_result\.get\("error"\)' src/saltmdb/viewer/routes/entity_detail.py
```
Must show **zero** matches -- confirms §7.2 Step 5's `is_rejected`/`result.get("data", {})` fix
landed and the old top-level reads are gone.

```bash
rg -n 'lineage_now\["total"\]' tests/test_mcp_tools.py
```
Must show **zero** matches -- confirms `test_graph_tools_honor_depth_limits` (§7.2 Step 6) now
reads `lineage_now["data"]["total"]`.

```bash
rg -n '"nodes": \[\{"id": successor' tests/test_get_memory.py
```
Must show **zero** matches -- confirms `test_returns_archived_entity_without_redirecting`'s mock
`return_value` (§7.2 Step 6) is now the enveloped shape (`{"status": "ok", "data": {"nodes":
[...]}, "warnings": []}`), not the bare pre-envelope dict.

```bash
rg -n '\.startswith\("Error"\)|\.startswith\("Relation |res\.startswith' src/saltmdb/ --glob '!telemetry_service.py'
```
Must show **zero** remaining matches anywhere in `src/saltmdb/` outside `telemetry_service.py` --
every string-prefix-sniffing call site identified in this spec (§6.3, §7.5, §7.6, §7.7, §11.1)
must be gone, and this search itself (broader than any single section's own citation) is the
safeguard against a call site this spec's own investigation missed (pre-lock gate step 3).
`telemetry_service.py:39`'s own `isinstance(result, str) and result.startswith("Error")` check is
excluded deliberately -- §15 keeps it in place as a defensive fallback for any future
non-conforming caller, not a bug this spec fixes; if this same grep without the `--glob` exclusion
is run instead, exactly one match (`telemetry_service.py:39`) is expected and is not a failure.

```bash
grep -c '"dependencies"' src/saltmdb/domain/services/relation_service.py
```
Must show the same count as on `develop` @ `8dd9102` **minus exactly 2** (the two references
removed from `get_related_memories`'s own return construction in §7.3) -- `analyze_dependencies`
itself keeps using this key name internally, so the count does not go to zero.

```bash
PYTHONPATH=src uv run python -c "
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.utils.envelope import is_ok, is_rejected
import sqlite3
conn = sqlite3.connect(':memory:')
init_db(conn)
r1 = memory_service.store_memory(title='T', content='C', tags=['x'], owner_id='o', db_connection=conn)
assert is_ok(r1), r1
eid = r1['data']['id']
r2 = memory_service.revise_memory(entity_id=eid, title='T2', content='C2', reason='fix', tags=None, db_connection=conn)
assert is_ok(r2), r2
assert r2['data']['inherited'].get('tags') == ['x'], r2
r3 = memory_service.revise_memory(title='T3', content='C3', reason='fix', db_connection=conn)
assert is_rejected(r3), r3
assert r3['errors'][0]['code'] == 'VALIDATION_ERROR' or r3['errors'][0]['field'] == 'entity_id', r3
print('OK')
"
```
Must print `OK` with no assertion error -- the concrete end-to-end regression test for this
spec's central fix (decision 1: tags inherit when omitted; decision 4: dispatch-layer validation
returns rather than raises), exercised through the real domain-service layer against a real
in-memory DB, not a mock.

```bash
grep -rn '"error_code"\]\|\["error_code"\]\|status.*REJECTED\|REJECTED.*status' src/ tests/ README.md AGENT_GUIDE.md
```
Must show **zero** matches (Amendment 1) -- every stale reference to the old uppercase
`"REJECTED"`/`error_code` capacity-gate shape (`mcp/tools.py:383`, `write.py:610`,
`README.md:193`, `README.md:234`, `AGENT_GUIDE.md:225`) must be updated to the new
`"rejected"`/`errors: [...]` shape, with no sixth reference surfacing anywhere else in the tree.

## Amendment 1

Independent verification pass (coordinator), performed by re-reading the actual source rather
than only this document's own text, found and this amendment fixes three issues before any
implementation began (no OMP work was in flight -- this is a pre-implementation correction, not
an adjudication of an in-progress `BLOCKED` report):

1. **§0 Scope-list gap**: `lifecycle.py`'s bullet cited `(§6, §8.2)` but omitted `§7.2`, which
   requires editing `_lineage_nodes` (lifecycle.py:42-58) as part of `get_lineage`'s envelope
   conversion. Confirmed real via `grep -n "_lineage_nodes" -A 20
   src/saltmdb/domain/services/memory_service/lifecycle.py`. **Fixed**: §0's `lifecycle.py`
   bullet now reads `(§6, §7.2, §8.2)`.

2. **§12.2's shape change left five stale documentation references uncorrected and two of them
   unlicensed**: §12.2's own suggested verification grep (`'"error_code"\]\|\["error_code"\]\|
   status.*REJECTED\|REJECTED.*status'`) was scoped to `src/ tests/` only, missing every `*.md`
   file entirely, and even the two Python docstring hits it *would* have caught
   (`mcp/tools.py:383`, `write.py:610`) were never licensed in §0's Scope list -- a strict-scope
   OMP could legitimately refuse to touch them despite §12.2's own prose describing the fix.
   Re-running the same grep pattern against `*.md` too found three more stale hits:
   `README.md:193`, `README.md:234`, and `AGENT_GUIDE.md:225` -- the third of these was
   specifically *not* found by the coordinator's own initial spot-check of `AGENT_GUIDE.md` and
   was only confirmed by independently re-running the grep during this amendment, per the
   coordinator's own instruction not to trust that spot-check. **Fixed**: §0's Scope list now
   licenses `mcp/tools.py`/`write.py`'s existing bullets for `§12.2` too, plus two new narrow
   bullets for `README.md` (lines 193, 234 only) and `AGENT_GUIDE.md` (line 225 only); §12.2
   itself now contains the exact before/after text for all five locations, plus a final
   re-verification grep command (extended to include `README.md AGENT_GUIDE.md` explicitly)
   confirming no sixth reference exists. Independently re-confirmed via a repo-wide
   `--include="*.md"` grep during this amendment: exactly these three `.md` hits exist, nothing
   else in the tree.

3. **§0 mislabel**: the `duplicates.py` Scope bullet described its cascading call site as
   "`store_relation`-shape-reading," but per §6.3 (`bulk_archive_memory`'s own call to
   `lifecycle.archive_memory`) it reads `archive_memory`'s return shape, not `store_relation`'s.
   **Fixed**: relabeled to "`archive_memory`-shape-reading."

**Amendment pre-lock re-check** (this workspace's spec-writing gate, applied to the amendment
itself, not just the original document): re-ran the affected acceptance-adjacent commands
against the current tree post-amendment-drafting --
`grep -n "_lineage_nodes" -A 20 src/saltmdb/domain/services/memory_service/lifecycle.py`
(confirms the function and line range cited in finding 1 are real and unchanged),
`grep -rn '"error_code"\]\|\["error_code"\]\|status.*REJECTED\|REJECTED.*status\|CORE_CAPACITY_EXCEEDED'
--include="*.md" .` (confirms exactly the three `.md` hits named in finding 2, nothing further,
excluding this spec file's own discussion of the same strings), and a full manual re-read of §0
against every section it now cross-references (§2 through §17, in order) to confirm every
section named in a Scope bullet actually exists and every file this document's mechanical
sections touch is licensed somewhere in §0 -- no further gap found. Status remains **LOCKED**.

## Amendment 2

OMP raised a genuine pre-implementation blocker (no implementation work was in flight -- adjudicated
before any file was touched, confirmed via `git status` in the implementation worktree showing a
clean tree) requiring a choice between (1) authorizing reader migrations in
`relation_service.py` (`analyze_lineage`), `lineage_assembly_service.py`, and
`conflict_set_service.py` plus their shape-dependent tests, or (2) amending §7.2 so `get_lineage`
keeps its raw internal shape and envelope-wraps only at a dispatch/tool boundary.

**Root cause**: §7.2's original "no cascading call site found" claim was based on `grep -rn
"get_lineage(" src/`, but that audit only reconciled the two callers it went on to discuss
(`daemon/dispatch.py`, `memory_service/lifecycle.py`) and never re-ran the grep against its own
full output. Re-running it during this amendment (`grep -rn 'get_lineage(' src/ | grep -v 'def
get_lineage'`) surfaced four more call sites the original text never accounted for:
`relation_service.py:959` (`analyze_lineage`'s own call, inside the *same file* this spec was
already editing), `conflict_set_service.py:118`/`:125`, `lineage_assembly_service.py:82`, and
`viewer/routes/entity_detail.py:48`. Cross-checked independently via `mcp__acie__find_references`
on `relation_service.py:get_lineage#function`, which returned the identical three in-repo service
call sites (plus four `tests/test_relation_service.py` call sites not caught by the plain grep,
since ACIE's reference graph includes test files by default) -- ACIE and grep agree on the
production call sites, giving two independent confirmations of the gap. Direct source read of
all three service call sites confirmed each does `"error" in result` / `result["nodes"]` against
`get_lineage`'s current bare-dict shape, which a `rejected()`/`envelope_ok()` conversion in place
would silently break (the `"error" in result` check would always be `False` against an envelope,
and `result["nodes"]` would `KeyError` since nodes move under `result["data"]`).

**Fix**: neither of OMP's two proposed options as originally framed -- both would either force
unnecessary test-shape migrations (option 1, if done as a naive in-place conversion) or push
envelope construction out of the domain-service layer against this spec's own architecture
(option 2, which would contradict decision 4's "validation lives in services, not dispatch"
principle already locked for every other function in this spec). Instead, §7.2 is rewritten to
apply the *existing* raw-core/enveloped-wrapper split this same file already uses for
`get_related_memories`/`analyze_dependencies` (§7.3): the current `get_lineage` body is renamed
verbatim to `_get_lineage_raw` (zero logic change), a new thin `get_lineage` wrapper is added
that calls it and converts the result via the same `if "error" in result: return
rejected([envelope_error(error_codes.VALIDATION_ERROR, result["error"])])` /
`return envelope_ok(result)` pattern §7.3 already established, and the three internal readers
plus four existing `tests/test_relation_service.py` call sites are redirected to call
`_get_lineage_raw` -- a pure rename at each call site, no shape-handling logic anywhere needs to
change, because the raw function's return shape is byte-identical to `get_lineage`'s
pre-this-spec shape. `dispatch.py` and `lifecycle.py` keep calling `get_lineage` (now the
wrapper) exactly as already locked. §0's Scope list gained two new file bullets
(`conflict_set_service.py`, `lineage_assembly_service.py`), both narrowly scoped to the one-line
rename only.

`viewer/routes/entity_detail.py`'s call site is the one call site this fix deliberately does
**not** touch, since `src/saltmdb/viewer/**` is already out of scope per §0's "Does not touch"
list -- documented as a known, accepted regression (silent empty ancestor panel, not a crash) in
§7.2's own text rather than left as an undiscovered side effect, with a note that a follow-up
spec should fix it the same way `_lineage_nodes` was fixed here.

**Amendment 2 pre-lock re-check**: re-ran `rg -n '\bget_lineage\(' src/saltmdb/domain/services/relation_service.py
src/saltmdb/domain/services/conflict_set_service.py src/saltmdb/domain/services/lineage_assembly_service.py`
against the current (pre-implementation) tree to confirm the call sites and line numbers cited
above are real and unchanged, and re-ran `mcp__acie__find_references` on
`relation_service.py:get_lineage#function` a second time after drafting this amendment's text to
confirm no fifth production call site was missed. Status remains **LOCKED**.

## Amendment 3

OMP hit a second BLOCKED wall on the same section. It correctly found that `viewer/routes/
entity_detail.py`'s `get_lineage` call site -- which Amendment 2 itself had already found and
named -- would fail two real tests (`tests/test_viewer_routes.py:864-891` and `:893-914`) once
the envelope conversion landed, directly contradicting §17's own "full suite must exit 0"
requirement. Amendment 2's resolution of that finding was wrong: it labeled the regression
"known, accepted" and left it unfixed because `viewer/**` was already out of scope, without
checking whether leaving it unfixed was actually compatible with §17's own acceptance gate. It
was not. This also directly contradicts this project's own standing rule (memory `74f6b4c0`):
a limitation is never accepted as long as it can be fixed within our own jurisdiction, and a
one-file, one-method envelope-unwrap fix plainly is. The correct move was to fix it using the
exact pattern already used for `_lineage_nodes` (Amendment 1) -- not to write it off.

**While re-investigating to fix this properly** (given this is the second BLOCKED report against
the same section, a full re-sweep was done rather than a point-fix), a broader pattern of
incompleteness in Amendment 2 itself was found: Amendment 2 enumerated the three internal
*service* readers correctly, but its Step 3 only described the *call-expression* rename
(`get_lineage(` -> `_get_lineage_raw(`) and never checked whether either `conflict_set_service.py`
or `lineage_assembly_service.py` imports `get_lineage` by name (`from
saltmdb.domain.services.relation_service import get_lineage`) -- both do. An import-statement
rename is a different edit from a call-expression rename (an unrenamed import left in place, or a
renamed import with an unrenamed call, either one is a real `ImportError`/`NameError` at collection
time, not a shape mismatch), and Amendment 2's text never distinguished the two. The same
incompleteness pattern extended to tests: Amendment 2 named the 4 `test_relation_service.py`
calls needing redirect but missed that `test_lineage_assembly_service.py` and
`test_conflict_set_service.py` each patch `get_lineage` as a name bound in the *importing*
module's own namespace (`unittest.mock.patch`'s "patch where it's used" convention) -- after the
import rename, patching a name that no longer exists there raises `AttributeError`, failing those
tests outright regardless of any shape fix. Also missed: `tests/test_get_memory.py`'s
`test_returns_archived_entity_without_redirecting`, whose mock `return_value` is the pre-envelope
bare shape and will silently defeat `_lineage_nodes`'s own Amendment-1 fix once that lands
(reading `result.get("data", {})` against a mock that has no `"data"` key at all yields nothing);
and `tests/test_mcp_tools.py:631`, which asserts `lineage_now["total"]` directly against
`tools.get_lineage(...)`'s real (non-mocked) return value through the full dispatch/service chain.

**Method used to find the complete set this time**, specifically to avoid a third BLOCKED report
on this same section: grepped `get_lineage`/`analyze_lineage` across the *entire* `tests/`
directory (not just the files Amendment 2 had already named) and read every hit's actual
surrounding code to classify it as import/call/patch-target/assertion and decide whether the new
shape breaks it; cross-checked the production call-site list a second time via
`mcp__acie__find_references`; and separately grepped for every remaining qualified
`relation_service.get_lineage` and `"get_lineage"` string reference in the whole tree to confirm
the MCP/dispatch-table tool-name string occurrences (`mcp/tools.py`, `daemon/protocol.py`,
`daemon/dispatch.py`'s `DISPATCH_TABLE` key) are unrelated to the function rename and need no
change. Every file/line named in §0's updated Scope list and §7.2 Steps 3, 5, and 6 was read
directly (not inferred) before being included.

**Fix**: §7.2 Step 5 (new) fixes `entity_detail.py`'s `get_lineage` block with the same
`is_rejected`/`result.get("data", {})` pattern already used for `_lineage_nodes`, licensed via a
narrow §0 Scope addition and a corresponding carve-out in the "Does not touch: `viewer/**`" line.
§7.2 Step 3 now explicitly separates import-statement renames from call-expression renames for
`conflict_set_service.py` and `lineage_assembly_service.py`. §7.2 Step 6 (renumbered from Step 5)
now enumerates, file by file, every test import/call/patch-target/assertion that needs to change
and explicitly confirms (by reading, not assuming) the ones that do not. §17 gained five new
acceptance greps covering the import renames, the patch-target renames, the viewer fix, and the
two newly found test-shape breaks.

**Amendment 3 pre-lock re-check**: re-ran every grep this amendment's own text cites (`rg -n
'get_lineage' src/saltmdb/domain/services/conflict_set_service.py
src/saltmdb/domain/services/lineage_assembly_service.py`, the full-tree import/qualified-reference
greps, and a fresh `mcp__acie__find_references` on `relation_service.py:get_lineage#function`)
against the current (still pre-implementation, confirmed via `git status`) tree, and manually
read every file this amendment names end to end rather than trusting the earlier session's
partial reads. Status remains **LOCKED**.
