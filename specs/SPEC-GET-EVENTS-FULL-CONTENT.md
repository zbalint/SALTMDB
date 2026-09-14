# SPEC-GET-EVENTS-FULL-CONTENT

## 0. Status

**LOCKED**

**Scope**: may edit `src/saltmdb/domain/services/event_service.py`,
`src/saltmdb/daemon/dispatch.py`, `src/saltmdb/mcp/tools.py`, and
`tests/test_mcp_tools.py` only. Does not touch: `src/saltmdb/db/schema.py` (no schema change --
`events.id` is already `TEXT PRIMARY KEY`), `src/saltmdb/daemon/protocol.py` (`get_events` is
already classified in `READ_TOOLS`; no new tool name, no per-param schema there to update),
`src/saltmdb/viewer/**` (the web viewer's `events.py` route already returns untruncated content
from the same table -- it is not part of this bug and must not be touched), `log_event` /
`dismiss_events` / any other function in `event_service.py`, `src/saltmdb/daemon/client.py`, and
any test file other than `tests/test_mcp_tools.py`.

## 1. Why

SALTMDB has a standing core memory (`0fb09b3f-0232-402a-9dc7-b52e1d6cbd7d`, title `[SALTMDB Bug]
get_events Truncates Long Event Content With No Full-Text Retrieval Path`) flagging that
`get_events` hard-truncates a returned event's `content` field at 1000 characters with no
parameter to request the full text, unlike `get_memory` (always returns complete content).
Investigated 2026-09-10 and confirmed **still live** in the current tree, not stale:

- `src/saltmdb/domain/services/event_service.py:149` --
  `display_content = econtent[:1000] + " [TRUNCATED]" if len(econtent) > 1000 else econtent` --
  unconditional, no override parameter anywhere in the function signature.
- The MCP-facing tool (`src/saltmdb/mcp/tools.py:969` `get_events`) and the dispatch layer
  (`src/saltmdb/daemon/dispatch.py:420` `_dispatch_get_events`) pass no such override through
  either -- the whole call chain has nowhere to plumb one.
- By contrast, `src/saltmdb/viewer/routes/events.py:80` (`"content": r[4]`) already returns raw,
  untruncated content from the *same* `events` table for the web viewer's read path. This proves
  the gap is specific to the MCP-facing tool/service function, not the storage layer -- there is
  no reason `get_events` (the MCP tool) needs to behave differently from the viewer route once
  full content is genuinely wanted.
- `src/saltmdb/daemon/protocol.py` classifies tools only by name (`READ_TOOLS`/`WRITE_TOOLS`);
  it does not validate or restrict a tool's kwargs, so adding new parameters carries no
  wire-protocol risk.

**No prior fix attempt exists for this** -- this is the first spec written against it. The core
memory itself names its own exit condition as "get_events gains a full-text/max_bytes retrieval
parameter (tool-level fix)"; this spec satisfies that condition.

**Chosen design** (confirmed with the user 2026-09-10, over two narrower alternatives -- a
flag-only fix, and an ID-filter-only fix): ship both, because they solve two different real
usage shapes and the codebase already has a precedent for exactly this two-tier split --
`search_memory` (cheap, snippet-bearing, lists many) vs. `get_memory` (explicit single-ID,
always full) is the identical pattern applied to memories; this spec applies the same pattern to
events:

1. **`full_content: bool = False`** on `get_events` -- when `True`, no row in the result is
   truncated. For the broad "list matching events" use case where the caller already knows the
   result set is small/bounded (e.g. narrowed by `context_id` to a handful of events) and wants
   all of them in full.
2. **`event_id: str | None = None`** on `get_events` -- an exact-match filter on the `id` column
   (primary key, so this matches at most one row), mirroring `get_memory`'s "explicit-ID
   retrieval is always full" contract. This is the direct fix for the exact recovery scenario
   the core memory describes: a caller sees a truncated event in a list result, has its `id`,
   and needs that one event's full content -- without needing to guess a `full_content=True`
   call is even safe to make against a potentially large unfiltered result set.
   **Matching `event_id` always returns full content for that row, regardless of the
   `full_content` flag's value** (same reasoning as `get_memory`: an explicit single-ID ask is
   never a "give me a truncated preview" ask).

Both defaults preserve exactly today's behavior for every existing caller (`full_content=False`,
`event_id=None`) -- this is purely additive.

## 2. `src/saltmdb/domain/services/event_service.py`

### 2.1 `get_recent_events` signature (currently lines 88-98)

Before:
```python
def get_recent_events(
    context_id: str = None,
    agent_id: str = None,
    type_filter: str = None,
    agent_session_id: str = None,
    order: str = "newest_first",
    limit: int = 20,
    offset: int = 0,
    db_connection=None,
    db_path: str = None,
) -> list:
```

After:
```python
def get_recent_events(
    context_id: str = None,
    agent_id: str = None,
    type_filter: str = None,
    agent_session_id: str = None,
    event_id: str = None,
    order: str = "newest_first",
    limit: int = 20,
    offset: int = 0,
    full_content: bool = False,
    db_connection=None,
    db_path: str = None,
) -> list:
```

### 2.2 Docstring (currently lines 99-107)

Before:
```python
    """Retrieves events from the append-only events ledger (agent API redesign plan §5.7,
    Phase 6 item 23).

    `context_id` is the headline filter (§3.3 fixed: previously stored on every row but
    unreachable from any agent-facing path). Every filter is a plain equality clause,
    including `agent_session_id` -- there is no more forced "session mode" (§3.4's `mode='session'`
    is gone) and no more dismissed-event/entity-status derivation loop (§3.5's `status_filter`
    is gone): this is now exactly one `SELECT ... LIMIT ? OFFSET ?`, ordered by `timestamp`
    ascending ("oldest_first") or descending ("newest_first", default).
    """
```

After (append two paragraphs, keep everything above unchanged):
```python
    """Retrieves events from the append-only events ledger (agent API redesign plan §5.7,
    Phase 6 item 23).

    `context_id` is the headline filter (§3.3 fixed: previously stored on every row but
    unreachable from any agent-facing path). Every filter is a plain equality clause,
    including `agent_session_id` -- there is no more forced "session mode" (§3.4's `mode='session'`
    is gone) and no more dismissed-event/entity-status derivation loop (§3.5's `status_filter`
    is gone): this is now exactly one `SELECT ... LIMIT ? OFFSET ?`, ordered by `timestamp`
    ascending ("oldest_first") or descending ("newest_first", default).

    `event_id` is an exact-match filter on the primary key (at most one row). Content is never
    truncated for a row matched by `event_id`, mirroring `get_memory`'s explicit-ID contract:
    an explicit single-ID ask is a deliberate full-content retrieval, never a preview.

    `full_content`, when True, disables the 1000-char truncation (see below) for every row in
    the result, not only an `event_id` match. Default False preserves today's truncated-preview
    behavior for ordinary list/discovery calls.
    """
```

### 2.3 WHERE-clause construction (currently lines 116-132)

Before:
```python
    try:
        where_clauses = []
        params: list = []
        if context_id:
            where_clauses.append("context_id = ?")
            params.append(context_id)
        if agent_id:
            where_clauses.append("agent_id = ?")
            params.append(agent_id)
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter)
        if agent_session_id:
            where_clauses.append("agent_session_id = ?")
            params.append(agent_session_id)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
```

After:
```python
    try:
        where_clauses = []
        params: list = []
        if context_id:
            where_clauses.append("context_id = ?")
            params.append(context_id)
        if agent_id:
            where_clauses.append("agent_id = ?")
            params.append(agent_id)
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter)
        if agent_session_id:
            where_clauses.append("agent_session_id = ?")
            params.append(agent_session_id)
        if event_id:
            where_clauses.append("id = ?")
            params.append(event_id)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
```

### 2.4 Truncation (currently lines 146-149)

Before:
```python
        results = []
        for r in cursor.fetchall():
            eid, etime, eagent, etype, econtent, ecode, esess, ectx = r
            display_content = econtent[:1000] + " [TRUNCATED]" if len(econtent) > 1000 else econtent
```

After:
```python
        results = []
        for r in cursor.fetchall():
            eid, etime, eagent, etype, econtent, ecode, esess, ectx = r
            skip_truncation = full_content or bool(event_id)
            display_content = (
                econtent
                if skip_truncation or len(econtent) <= 1000
                else econtent[:1000] + " [TRUNCATED]"
            )
```

No other lines in this file change. `dismiss_events` and `log_event` are untouched.

## 3. `src/saltmdb/daemon/dispatch.py`

### 3.1 `_dispatch_get_events` (currently lines 420-429)

Before:
```python
def _dispatch_get_events(**kw):
    return event_service.get_recent_events(
        context_id=kw.get("context_id"),
        agent_id=kw.get("agent_id"),
        type_filter=kw.get("event_type"),
        agent_session_id=kw.get("agent_session_id"),
        order=kw.get("order") or "newest_first",
        limit=kw.get("limit") or 20,
        offset=kw.get("offset") or 0,
    )
```

After:
```python
def _dispatch_get_events(**kw):
    return event_service.get_recent_events(
        context_id=kw.get("context_id"),
        agent_id=kw.get("agent_id"),
        type_filter=kw.get("event_type"),
        agent_session_id=kw.get("agent_session_id"),
        event_id=kw.get("event_id"),
        order=kw.get("order") or "newest_first",
        limit=kw.get("limit") or 20,
        offset=kw.get("offset") or 0,
        full_content=bool(kw.get("full_content")),
    )
```

No other lines in this file change.

## 4. `src/saltmdb/mcp/tools.py`

### 4.1 `get_events` (currently lines 968-1002)

Before:
```python
@mcp.tool()
def get_events(
    context_id: str | None = None,
    agent_id: str | None = None,
    event_type: str | None = None,
    agent_session_id: str | None = None,
    order: Literal["newest_first", "oldest_first"] = "newest_first",
    limit: int | None = None,
    offset: int | None = None,
) -> list:
    """Retrieves events from the append-only events ledger, for multi-agent coordination and
    wrap-up thread review.

    `context_id` is the headline filter -- reading back every event logged under a shared
    thread handle, survivable across a power cut. `agent_id` filters to one agent's events, for
    "what did the other agent just decide" in a multi-agent DB (this tool has no notion of "my
    own events" the way log_event has a bound owner -- it is a read across the whole ledger,
    narrowed by whichever filters are supplied). `agent_session_id` filters to one SALTMDB
    adapter-process session.

    `order`: "newest_first" (default, for discovery) or "oldest_first" (for chronological
    wrap-up synthesis) -- always explicit, never inferred from which filter was passed.
    """
    return _backend_or_raise().call(
        "get_events",
        {
            "context_id": context_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "agent_session_id": agent_session_id,
            "order": order,
            "limit": limit if limit is not None else 20,
            "offset": offset if offset is not None else 0,
        },
    )
```

After:
```python
@mcp.tool()
def get_events(
    context_id: str | None = None,
    agent_id: str | None = None,
    event_type: str | None = None,
    agent_session_id: str | None = None,
    event_id: str | None = None,
    order: Literal["newest_first", "oldest_first"] = "newest_first",
    limit: int | None = None,
    offset: int | None = None,
    full_content: bool = False,
) -> list:
    """Retrieves events from the append-only events ledger, for multi-agent coordination and
    wrap-up thread review.

    `context_id` is the headline filter -- reading back every event logged under a shared
    thread handle, survivable across a power cut. `agent_id` filters to one agent's events, for
    "what did the other agent just decide" in a multi-agent DB (this tool has no notion of "my
    own events" the way log_event has a bound owner -- it is a read across the whole ledger,
    narrowed by whichever filters are supplied). `agent_session_id` filters to one SALTMDB
    adapter-process session.

    `order`: "newest_first" (default, for discovery) or "oldest_first" (for chronological
    wrap-up synthesis) -- always explicit, never inferred from which filter was passed.

    Content over 1000 characters is truncated with a "[TRUNCATED]" suffix by default. Two ways
    to get the full text back, mirroring get_memory's list-then-fetch-by-ID pattern:
    `event_id` (exact match on the primary key, at most one row) always returns that row's
    content in full, regardless of `full_content`. `full_content=True` disables truncation for
    every row a broader query matches -- use it once the other filters (e.g. `context_id`)
    already bound the result set to something you know is safe to receive in full.
    """
    return _backend_or_raise().call(
        "get_events",
        {
            "context_id": context_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "agent_session_id": agent_session_id,
            "event_id": event_id,
            "order": order,
            "limit": limit if limit is not None else 20,
            "offset": offset if offset is not None else 0,
            "full_content": full_content,
        },
    )
```

No other lines in this file change.

## 5. `tests/test_mcp_tools.py`

Add the following test methods to `TestGetEventsEndToEnd` (currently ending at line 1811 with
`test_removed_kwargs_raise_type_error`). Insert them immediately after that method, before the
class's closing (i.e. before `class TestLogEventEndToEnd(unittest.TestCase):` at line 1814).
This is a pure addition -- no existing test in this class changes.

```python
    def test_full_content_flag_returns_untruncated_content(self):
        long_content = "x" * 1500
        tools.log_event(event_type="issue", content=long_content)

        truncated = tools.get_events(agent_id="test_agent")
        self.assertTrue(truncated[0]["content"].endswith("[TRUNCATED]"))
        self.assertEqual(len(truncated[0]["content"]), len("x" * 1000 + " [TRUNCATED]"))

        full = tools.get_events(agent_id="test_agent", full_content=True)
        self.assertEqual(full[0]["content"], long_content)
        self.assertNotIn("[TRUNCATED]", full[0]["content"])

    def test_default_still_truncates_long_content(self):
        # Regression guard: adding full_content/event_id must not change the existing default
        # (full_content omitted) truncation behavior for ordinary list calls.
        long_content = "y" * 2000
        tools.log_event(event_type="issue", content=long_content)

        events = tools.get_events(agent_id="test_agent")
        self.assertTrue(events[0]["content"].endswith("[TRUNCATED]"))
        self.assertLess(len(events[0]["content"]), len(long_content))

    def test_event_id_filter_returns_single_full_content_event(self):
        long_content = "z" * 1500
        tools.log_event(event_type="issue", content=long_content)
        tools.log_event(event_type="issue", content="short other event")

        # Discover the long event's id the way a real caller would: from a truncated list result.
        listed = tools.get_events(agent_id="test_agent", event_type="issue")
        target_id = next(e["id"] for e in listed if e["content"].endswith("[TRUNCATED]"))

        matched = tools.get_events(event_id=target_id)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["id"], target_id)
        self.assertEqual(matched[0]["content"], long_content)
        self.assertNotIn("[TRUNCATED]", matched[0]["content"])

    def test_event_id_filter_no_match_returns_empty_list(self):
        tools.log_event(event_type="issue", content="irrelevant")
        matched = tools.get_events(event_id="00000000-0000-0000-0000-000000000000")
        self.assertEqual(matched, [])
```

## 6. Out of scope

- Any change to `src/saltmdb/db/schema.py` -- `events.id` is already `TEXT PRIMARY KEY`, no
  migration needed.
- Any change to `src/saltmdb/daemon/protocol.py` -- `get_events` is already in `READ_TOOLS`, and
  the protocol layer validates tool names only, never per-tool kwargs.
- Any change to `src/saltmdb/viewer/**` (viewer routes, `_protocol.py`, `base.py`) -- the web
  viewer's own `events.py` route already returns untruncated content; it is not part of this bug
  and must not be touched or "aligned" as a drive-by.
- Any change to `log_event`, `dismiss_events`, or any other function in `event_service.py`.
- Any change to `daemon/client.py` or any other test file besides `tests/test_mcp_tools.py`.
- Recovery of any already-truncated pre-existing event in the live ledger. That remains governed
  by the direct-SQL-override precedent in memory `9185b06b` (flag the SQL-access-ban default,
  get explicit real-time user authorization) -- this spec fixes the tool going forward, it does
  not retroactively recover old truncated rows, and does not touch the SQL-access-ban default.
  Once this ships, a *newly* truncated event found in a list result is recoverable via
  `event_id` without needing that override path at all -- but that is a consequence of the fix,
  not a separate deliverable in it.
- Raising or otherwise reconfiguring the 1000-char default truncation threshold itself. Out of
  scope by design: the point of this spec is an opt-in override, not a change to the default.
- Any change to the `1000` constant's value, or extracting it into a named constant/config
  value -- purely additive parameters only, no refactor of the existing truncation line beyond
  what's needed to gate it.

## 7. Acceptance

Run from the repo root of the worktree:

```bash
rg -n '"\[TRUNCATED\]"' src/saltmdb/domain/services/event_service.py
```
Must show exactly one occurrence, still inside `get_recent_events`, now reachable only when
`skip_truncation` is falsy (i.e. `full_content` is falsy and `event_id` is not supplied).

```bash
rg -n "full_content|event_id" src/saltmdb/mcp/tools.py src/saltmdb/daemon/dispatch.py src/saltmdb/domain/services/event_service.py
```
Must show `full_content` and `event_id` present in all three files' `get_events`/
`_dispatch_get_events`/`get_recent_events` signatures and bodies (per §2-§4 above).

```bash
python -m pytest tests/test_mcp_tools.py -k "GetEvents" -v
```
Must pass, including the four new tests added in §5, with zero regressions in the existing
`TestGetEventsEndToEnd` methods (context/agent/type/session filters, ordering, pagination, and
the `test_removed_kwargs_raise_type_error` regression guard).

```bash
python -m pytest -q
```
Full suite must pass (no regression anywhere else in the tree).
