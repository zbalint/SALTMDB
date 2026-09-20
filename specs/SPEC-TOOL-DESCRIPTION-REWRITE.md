# SPEC-TOOL-DESCRIPTION-REWRITE

## 0. Status

**LOCKED, INCLUDES AMENDMENT 1 -- PRECONDITION SATISFIED**

This spec's original hard precondition -- `SPEC-TOOL-CONTRACT-CONSISTENCY.md` (Spec 1) implemented,
its acceptance suite passing, merged into `develop` -- is now satisfied: Spec 1 shipped as commit
`cca563e`, merged `e8ce355`, full suite re-verified post-merge (`1726 passed, 18 subtests passed,
0 failed`), pushed to `origin/develop`. A live regression (7 of `tools.py`'s return-type
annotations left stale relative to Spec 1's runtime changes, breaking the real FastMCP call path
for `log_event`/`search_tags`/`list_predicates`/`merge_tags`/`archive_memory`/
`review_core_memory`/`update_memory_metadata`) was found and hotfixed separately, directly, as
commit `d214433` -- unrelated to this spec's own docstring-content scope, already deployed live,
not part of this document's remaining work. Every fact this document states about a tool's return
shape, error codes, or parameter optionality traces to Spec 1's actual final locked text
(including its own Amendments 1-4) -- re-verified directly against the real shipped source at
`develop@e8ce355` in Amendment 1 below, which found and fixed two drifts introduced by Spec 1's
own Amendment 2 landing after this document was originally drafted. This spec's worktree and OMP
handoff may now proceed.

**Scope**: may edit:
- `src/saltmdb/mcp/tools.py` -- every one of the 19 `@mcp.tool()` function docstrings (and, where
  named in a section below, the function's return type annotation only -- no parameter is added,
  removed, renamed, or retyped, and no runtime logic changes anywhere in this file).

**Does not touch**: any file outside `mcp/tools.py`. In particular: `daemon/dispatch.py`,
every `domain/services/**` file, `utils/envelope.py`, `utils/error_codes.py`, `README.md`,
`AGENT_GUIDE.md` (already brought current by Spec 1 Amendment 1's own narrow fix -- a second,
broader pass over either file, if wanted, is a separate future spec, not this one), the
`saltmdb-usage` skill, and any test file. No parameter is added to any tool's signature (this is
not the place to add `has_more`, expose `owner_id`, or otherwise grow a schema -- see §21).

## 1. Why

zbalint's standing SALTMDB design goal: any agent must be able to use SALTMDB correctly as an MCP
server purely from the 19 exposed `mcp__saltmdb__*` tools' descriptions/schemas, without ever
needing to read the `saltmdb-usage` skill or any `AGENTS.md`/`CLAUDE.md`. This was tested directly
on 2026-09-15: zbalint asked Codex to attempt exactly that, deliberately withholding the skill
(SALTMDB agent session `01a0a2a9-f479-707c-bce7-89c75b753cbb`). The resulting audit (memories
`841b2cfe`, `022e7c45`, `1446c55c`, `e62d47ee`) found real, reproducible friction: a schema/runtime
mismatch on `revise_memory`'s `tags` parameter, return shapes an agent could not reliably classify
as success or failure from the description alone, and tool-choice ambiguity between
`search_memory`/`retrieve_context`.

`SPEC-TOOL-CONTRACT-CONSISTENCY` (Spec 1) fixed the underlying **runtime**: all 19 tools now
return a small number of well-defined shapes (mostly the shared `{"status": "ok"/"rejected",
"data"/"errors": [...], "warnings": [...]}` envelope, with two fully-bare-shape exceptions and
three single-vs-bulk-mode exceptions, all enumerated per-tool below), `tags` is genuinely optional
on `revise_memory`/`supersede_memory`, `list_predicates` carries real disposition data, and
`get_related_memories` no longer exposes a duplicate `dependencies` key. This spec's own job is
narrower and purely textual: rewrite every one of the 19 tools' docstrings (and, where the current
annotation is provably stale against Spec 1's shipped behavior, its return type annotation) so
that an agent reading only the schema and docstring -- no skill, no source read -- can correctly
answer, for every tool: is this the right tool for my task (and if not, which is)? What must I
supply? What comes back on success, and on failure -- and can I tell which happened by inspecting
the result? How do I recover from a failure? A worked example where the shape isn't obvious from
the signature alone.

**Template** (locked, memory `022e7c45`): **Purpose -> tool choice -> inputs -> consequences ->
recovery -> example**. Put the ordinary path first; move historical implementation commentary
(governance-gate internals, migration notes like `manage_relation`'s "Previously the bulk shape
silently ignored...") out of the primary description into a form a first-read doesn't have to
wade through -- **this spec keeps such notes** (per Coding Standards rule 6, not deleting
information without a reason beyond tidiness) **but moves each one to the end of its tool's
docstring**, after the recovery/example material, so the ordinary-use path is never interrupted by
implementation history a first-time caller does not need. Reciprocal tool-choice framing for
`search_memory`/`retrieve_context` is separately locked (memory `1446c55c`) and applied in §7/§17
below.

**Precondition inventory -- exactly what Spec 1 changed, verified against its final locked text
(including its own Amendment 1) before writing a single line of any docstring below:**

| Tool | Pre-Spec-1 return shape | Post-Spec-1 return shape | Spec 1 section |
|---|---|---|---|
| `log_event` | bare string | envelope (`ok`/`rejected`) | §11.1 |
| `search_tags` | bare list, `[{"error":...}]` on fault | envelope (`ok`/`rejected`) | §9.1 |
| `list_predicates` | bare list, no disposition data | envelope `ok([{id,name,disposition,lifecycle_tool}, ...])` | §7.1 |
| `merge_tags` | bare string | envelope (`ok`/`rejected`) | §10.1 |
| `store_memory` | envelope on most paths, bare string on 2 fallback paths | envelope on **every** path | §5.2, §8.1 (happy path was already envelope-adopted pre-Spec-1) |
| `search_memory` | bare `list[dict]`, `[{"error":...}]` sentinel on fault | **unchanged** -- explicitly deferred (§16 of Spec 1) | not touched |
| `archive_memory` (single/none mode) | bare string | envelope (`ok`/`rejected`) | §6.3 |
| `archive_memory` (bulk/list mode) | bare list of per-item dicts | **unchanged** -- only the *internal* reading of the now-enveloped single-item shape was fixed (§6.3's cascading call site); the bulk function's own external shape is untouched | §6.3 (cascading note only) |
| `manage_relation` (single mode) | bare string | envelope (`ok`/`rejected`), via `store_relation`/`invalidate_relation` | §7.5, §7.6 |
| `manage_relation` (bulk `relations` mode) | bare list of per-item dicts | **unchanged** external shape (internal string-sniffing fixed, §7.7) | §7.7 |
| `manage_relation` (predicate pre-flight rejection, either mode) | already `rejected()` dict | unchanged | §5.3 (cited as already-correct reference) |
| `consolidate_memories` (single mode) | hand-rolled dict, byte-identical to envelope shape | real `envelope.ok()`/`rejected()` calls, byte-identical output | §7.4 |
| `consolidate_memories` (bulk `consolidations` mode) | bare list of per-item dicts | **unchanged** | not touched |
| `revise_memory` / `supersede_memory` | already envelope-adopted; `tags` omission raised an uncaught exception client-side (`DaemonRpcError`, not a returned value -- see below) | envelope-adopted, `tags` genuinely optional (inherits from predecessor when omitted); **no more raised exception for any validation failure -- every failure is now a clean returned `rejected()` value** | §4.2, §4.3, §5.1, §6.1, §6.2 |
| `get_memory` | already envelope-adopted | unchanged | not touched |
| `inspect_memory` | already envelope-adopted | unchanged | not touched |
| `get_lineage` | bare dict, `{"error": "..."}` on validation failure, unwrapped node data on success | envelope-wrapped: `rejected()` on failure, `ok({...node data...})` on success (node data now under `data`, not top-level) | §7.2 |
| `get_related_memories` | bare dict, both a `dependencies` key and a `related_memories` key (identical duplicate lists) | envelope-wrapped (`rejected()` on failure, `ok(payload)` on success); `dependencies` key is **gone**, only `related_memories` remains | §7.3 |
| `retrieve_context` | bare bespoke dict on success; raised exception (not a returned value) on invalid `query` | success path **unchanged** (still the bare, non-enveloped `{query, memories, edges, lineage, conflict_sets, metadata}` dict -- Spec 1 does not touch `assemble_retrieve_context`); invalid `query` now returns a clean `rejected()` envelope instead of raising | §4.5 |
| `get_events` | bare list | **unchanged** -- not in Spec 1's scope at all | not touched |
| `review_core_memory` | bare string | envelope (`ok`/`rejected`) | §12.1 |
| `update_memory_metadata` | bare string | envelope (`ok`/`rejected`) | §6.4 |

**Critical asymmetries a rewrite must not paper over**: `archive_memory`, `manage_relation`, and
`consolidate_memories` each have **two different return shapes depending on which mode the
caller's own argument shape selects** -- a single-item call returns the standard envelope, a
batch call (a list-valued `entity_id`/`relations`/`consolidations`) returns a bare list of
per-item result dicts, entirely untouched by Spec 1. Confirmed directly against
`daemon/dispatch.py`'s `_dispatch_archive_memory` (line 222) and `_dispatch_consolidate_memories`
(line 304), and `domain/services/memory_service/duplicates.py`'s `bulk_archive_memory` (line 280,
`-> list`) and `relation_service.py`'s `bulk_store_relations`/`bulk_commit_consolidation` (both
`-> list`) -- none of these three bulk functions' own return type is changed anywhere in Spec 1. A
docstring that says "returns the standard envelope" without qualifying "only when called with a
single id/relation/consolidation set, not a list" would be a **new** inaccuracy this very spec
exists to prevent.

## 2. `log_event`

Before (tools.py:271-299, docstring only, lines 278-288):

```python
    """Appends an event to the append-only events ledger.

    The bound owner (§4.5's per-session identity) is injected as the event's `agent_id` --
    log_event has no separate agent_id parameter of its own. `get_events` keeps `agent_id` as a
    FILTER for cross-agent coordination, which is why the two tools are asymmetric on this
    field: an agent always logs as itself, but may read what any agent logged.

    `agent_session_id` is not a parameter here either -- the adapter auto-populates it from its
    own process-local session identity.

    """
```

Return type annotation before: `-> str` (line 277). After: `-> dict` (always the envelope shape
post-Spec-1, §11.1 -- no path in `log_event` returns a bare string anymore).

After:

```python
    """Appends one entry to the append-only, immutable events ledger -- for logging your own
    decisions/issues/fixes/attempts in real time, and for multi-agent coordination via a shared
    context_id thread. Not for storing durable knowledge: a fact/decision/procedure worth
    finding again later belongs in store_memory instead; log_event is a lightweight, append-only
    trace, not indexed for semantic search the way memories are.

    Required: event_type (a short free-text label, e.g. "decision"/"issue"/"fix"/"attempt" --
    not schema-constrained, but consistent labels make get_events' event_type filter useful) and
    content (the event body; PII/secrets are scrubbed server-side before storage). Optional:
    context_id (a shared thread handle so another agent's get_events(context_id=...) reads back
    this same event) and error_code (a short machine-readable label for an `issue`-type event,
    read back by get_events, not validated against any enum).

    You are never asked for agent_id or agent_session_id -- both are bound automatically from
    the calling adapter's own configured identity and process session; get_events uses agent_id
    as a read-side FILTER for "what did another agent log," which is why this write-side tool has
    no equivalent parameter of its own.

    Returns `{"status": "ok", "data": {"id": <event id>, "message": "..."}, "warnings": []}` on
    success. A failure (an unexpected server-side fault -- there are no caller-input validation
    checks on this tool beyond the two required parameters, enforced by the schema itself)
    returns `{"status": "rejected", "errors": [{"code": "INTERNAL_ERROR", "message": "..."}],
    "warnings": []}`; check `result["status"]` before trusting `result["data"]`.

    Example: `log_event(event_type="decision", content="Chose X over Y because...",
    context_id="my-session-thread")`.
    """
```

## 3. `search_tags`

Before (tools.py:302-311, docstring only, lines 304-308):

```python
    """Queries the database to suggest existing canonical tags matching a search query/substring, to prevent tag fragmentation. Use query='auth' to filter by tag name substring.

    Advisory discovery, not a prerequisite -- tags need not pre-exist; a new domain still
    creates a tag automatically on write. limit caps the number of tags returned (default 50),
    including when query is omitted -- the full canonical tag table is never dumped unbounded."""
```

Return type annotation before: `-> list` (line 303). After: `-> dict` (always the envelope shape
post-Spec-1, §9.1 -- including an explicit `ok([])` for zero matches, no longer a bare `[]`).

After:

```python
    """Looks up existing canonical tags by a name substring, to avoid creating a near-duplicate
    tag (e.g. searching before inventing a new one). Advisory only, never required before a
    write: store_memory/revise_memory/etc. auto-create any tag that doesn't already exist.

    query (optional) filters by substring, e.g. query="auth" matches "#auth-flow". Omitting it
    lists canonical tags up to limit (default 50) -- never an unbounded dump.

    Returns `{"status": "ok", "data": [{"id": ..., "name": ...}, ...], "warnings": []}` --
    `data` is `[]`, not an error, when nothing matches. A genuine fault returns `{"status":
    "rejected", "errors": [{"code": "INTERNAL_ERROR", "message": "..."}]}`.

    Example: `search_tags(query="auth")` before tagging a new memory `#auth-refactor`, to check
    whether `#auth`/`#authentication` already exists as the canonical form.
    """
```

## 4. `list_predicates`

Before (tools.py:314-329, docstring only, lines 316-326):

```python
    """Lists the closed relation-predicate vocabulary manage_relation accepts, optionally
    filtered by a search substring (e.g. query='resolve').

    11 agent-selectable predicates (elaborates_on, related_to, resolves, depends_on, verifies,
    corrects, caused_by, derived_from, distinguishes_from, part_of, contradicts); 3 reserved/
    system-owned predicates (supersedes, consolidated_from, revises) created only by their
    matching lifecycle tool; and similar_to, legacy and read-only. A non-canonical or drifted
    spelling submitted to manage_relation is rejected with a corrected call rather than silently
    accepted, so this list is advisory discovery, not something an agent must memorize.

    limit caps the number of predicates returned (default 50)."""
```

Return type annotation before: `-> list` (line 315). After: `-> dict` (always the envelope shape
post-Spec-1, §7.1).

After:

```python
    """Lists the closed relation-predicate vocabulary manage_relation accepts, each tagged with
    its own disposition -- so you can tell a predicate you may use from one you may only read,
    without memorizing the fixed lists below. Advisory discovery, not a prerequisite: submitting
    an unrecognized or drifted predicate spelling to manage_relation is rejected with a
    corrected_call using the right one, so you don't have to call this tool first.

    query (optional) filters by substring, e.g. query="resolve" matches "resolves". limit caps
    results (default 50).

    Returns `{"status": "ok", "data": [{"id", "name", "disposition", "lifecycle_tool"}, ...]}`.
    `disposition` is one of `"selectable"` (pass it to manage_relation freely), `"reserved"`
    (created only by the tool named in `lifecycle_tool` -- e.g. `supersedes` by supersede_memory,
    `consolidated_from` by consolidate_memories, `revises` by revise_memory -- manage_relation
    rejects a direct attempt), or `"legacy_readonly"` (existing edges remain visible; no new ones
    may be created -- currently only `similar_to`). This tool's own query only ever returns
    canonical predicates, so `disposition` is never `"alias"` here even though manage_relation's
    own drift-correction logic recognizes aliases too -- an alias spelling isn't itself a row in
    the canonical table this tool lists.

    A fault returns `{"status": "rejected", "errors": [{"code": "INTERNAL_ERROR", ...}]}`.

    Example: `list_predicates(query="depend")` to confirm `depends_on` is the right spelling
    before calling `manage_relation(predicate="depends_on", ...)`.
    """
```

## 5. `merge_tags`

Before (tools.py:332-343, docstring only, lines 337-339):

```python
    """Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all
    affected entities' tag associations. Use to fix folksonomy fragmentation (e.g. keep_tag='#docs',
    tags_to_merge=['#doc', '#documentation'])."""
```

Return type annotation before: `-> str` (line 336). After: `-> dict` (always the envelope shape
post-Spec-1, §10.1).

After:

```python
    """Merges one or more fragmented/synonym tags into a single canonical tag, repointing every
    affected memory's tag associations. Use when you've noticed folksonomy drift -- e.g. some
    memories tagged '#doc', others '#documentation' -- and want one canonical spelling going
    forward.

    keep_tag: the canonical tag every merged tag repoints to; it must already exist. tags_to_merge:
    one or more existing tag names/IDs to fold into keep_tag (accepts a list or a
    comma-separated string).

    Returns `{"status": "ok", "data": {"merged": [...], "skipped": [...], "canonical_tag":
    keep_tag}}` on success -- `skipped` lists any requested tag that didn't exist or was already
    canonical, without failing the whole call. `{"status": "rejected", "errors": [{"code":
    "NOT_FOUND", "message": "...", "field": "keep_tag"}]}` when keep_tag itself doesn't exist --
    search_tags first if you're not certain of the exact existing spelling.

    Example: `merge_tags(keep_tag="#docs", tags_to_merge=["#doc", "#documentation"])`.
    """
```

## 6. `store_memory`

Before (tools.py:346-403, the `@mcp.tool(description=...)` kwarg -- quoted in full since this
tool uses the explicit-`description=` style, not a plain docstring):

```python
@mcp.tool(
    description="""Stores a consolidated Markdown fact chunk as long-term memory.

    memory_type classifies the memory into one of five fixed kinds (default 'fact' on a new
    memory; omitting it on an update preserves the existing value, same as is_core):
      - fact: semantic, durable, generalized knowledge.
      - event: episodic, something that happened, ideally timestamped.
      - procedure: how-to / runbook / skill.
      - decision: ADR-style rationale record (what was chosen and why).
      - preference: durable user/agent preference statement.

    is_core is the single writable source of truth for "always load at session bootstrap."
    The '#core' tag is a derived label the server auto-maintains from is_core on every write --
    do not set '#core' via the tags list, it will be silently overridden to match is_core.

    entity_id (optional) targets an existing memory directly for an update -- when supplied it
    takes precedence over the automatic same-title/owner/scope match and bypasses the
    exact-content-hash duplicate check, so a metadata-only edit (e.g. updating metadata, re-tagging,
    backfilling core_reason/core_exit_condition) doesn't require changing content. Submitted
    metadata keys are shallow-merged into the existing stored metadata object (existing keys not
    mentioned in the new call, like a possible future search_aliases or project_id key, are
    preserved, not wiped).

    Exact content-hash duplicates are rejected with the existing entity ID. Near duplicates are
    stored and returned inline as `duplicate_candidates`, with guidance to call
    `supersede_memory` or `consolidate_memories` when the relationship is confirmed, or
    `manage_relation` to link them when related but not actually redundant.

    Core-memory bootstrap governance: `is_core=True` marks a memory for injection into every
    future session's bootstrap context -- it is a SCARCE, TEMPORARY mechanism for urgent
    cross-session hazards an agent must know before it could reasonably search for them, not a
    general "important knowledge" tier. Stable coding rules/preferences belong in AGENTS.md/
    CLAUDE.md/skills instead. Creating or promoting a core memory requires `scope="shared"` (no
    private cores), `core_reason` (20-500 chars: the harm that could occur before natural
    retrieval), `core_exit_condition` (20-500 chars: the observable condition that ends the
    urgency), and admits three independent hard caps: at most 5 active cores globally, at most
    2,500 Unicode characters of `content` per core, and a 15,000-character exact rendered
    bootstrap digest. A capacity failure returns `status: "rejected"` with `errors[0].code ==
    "CORE_CAPACITY_EXCEEDED"`, a balanced inventory of every active core (no full content), and
    zero side effects -- rebalance (demote/archive/shorten/consolidate existing cores) and retry;
    this never requires a human decision. `core_review_after` defaults to 14 days out and may
    never exceed 30 days; while ANY core is overdue for review, creating/promoting a new core,
    enlarging an existing core's content, or changing its review timestamp is blocked (use
    `review_core_memory` to resolve the overdue review first) -- demote/archive/non-expanding
    edits stay allowed. Omitting `core_reason`/`core_exit_condition`/`core_review_after`/
    `detail_memory_ids` on an update to an already-core memory preserves the existing values;
    supplying any of them when the effective memory is NOT core is rejected, never silently
    ignored. `detail_memory_ids` (at most 3 full UUIDs of existing shared, non-core memories whose
    canonical title+UUID must appear in `content`) atomically maintains `elaborates_on` edges from
    each detail memory to this core -- `None` preserves the current declaration, `[]` clears it.
    A core must stay directly actionable on its own even if a weaker agent never follows a detail
    link; move rationale/chronology/evidence into the linked detail memories instead.

    Exactly one of `content` or `content_file_path` is required: pass `content_file_path` (a
    local file SALTMDB reads server-side) instead of inlining a large body -- avoids
    round-tripping a large string through the calling agent's own context just to pass it here.

    """
)
```

(Note: the `status: "REJECTED"`/`error_code` wording shown above already reflects Spec 1
Amendment 1's fix at this exact location -- this section's "Before" is Spec 1's own "After," not
the original pre-Spec-1 text, since Spec 1 has not shipped yet as of this document's own
drafting; both specs describe the same eventual file, at different points in its edit history.)

Return type annotation before: `-> str | dict` (line 421). After: `-> dict` (always the envelope
shape post-Spec-1 -- §5.2 fixed the last bare-string fallback; `_resolve_content`'s and the
front-matter-identity check's own rejections were already `rejected()` dicts pre-Spec-1).

After (moving the ordinary path first, per-template; governance/historical detail retained but
pushed after the core mechanics, consequences, and recovery guidance):

```python
    description="""Creates a new long-term memory (a fact/event/procedure/decision/preference),
    or performs a metadata-only update to an existing one via entity_id. For CORRECTING or
    REPLACING an existing memory's content, use revise_memory/supersede_memory instead -- this
    tool's entity_id path only ever touches metadata/tags/governance fields, never content.

    Required: title, and exactly one of content or content_file_path (a local file path SALTMDB
    reads server-side -- use this instead of inlining a large body to avoid round-tripping it
    through your own context). memory_type classifies the memory (default 'fact'; omitted on an
    entity_id update, the existing value is preserved): fact (durable general knowledge), event
    (something that happened), procedure (how-to/runbook), decision (ADR-style rationale),
    preference (a durable user/agent preference).

    entity_id (optional) targets an existing memory for a metadata-only update -- re-tagging,
    updating `metadata` (shallow-merged, existing keys not mentioned are preserved), or
    backfilling core-governance fields -- without needing content unchanged-and-restated.

    Returns `{"status": "ok", "data": {"id": ..., "duplicate_candidates": [...] | None, ...},
    "warnings": [...]}` on success. Rejections you'll see in practice: an exact content-hash
    duplicate (`errors[0].code == "REJECT_EXACT_DUPLICATE"`, naming the existing entity -- a near
    duplicate is NOT rejected, it stores and returns `duplicate_candidates` inline instead, with
    guidance to call supersede_memory/consolidate_memories once you've confirmed the
    relationship, or manage_relation to link without merging); `title`/`tags` found inside YAML
    front matter in `content` (`IDENTITY_IN_YAML_FRONT_MATTER`, with a ready-to-resubmit
    `corrected_call`); neither or both of `content`/`content_file_path` supplied
    (`MISSING_CONTENT`/`CONTENT_AND_FILE_PATH_BOTH_SET`); an unreadable/non-UTF-8
    `content_file_path` (`CONTENT_FILE_READ_FAILED`).

    Example: `store_memory(title="[Project] Decision to use X", content="...", tags=["arch"],
    memory_type="decision")`.

    **Core-memory bootstrap governance** (`is_core`): a SCARCE, TEMPORARY mechanism for urgent
    cross-session hazards an agent must know before it could reasonably search for them -- not a
    general "important knowledge" tier (stable rules/preferences belong in AGENTS.md/CLAUDE.md/
    skills instead). `is_core=True` requires `scope="shared"` plus `core_reason`/
    `core_exit_condition` (20-500 chars each) and admits three hard caps: at most 5 active cores
    globally, 2,500 chars of content each, and a 15,000-char rendered bootstrap digest. A capacity
    failure returns `{"status": "rejected", "errors": [{"code": "CORE_CAPACITY_EXCEEDED", ...}],
    "violated_dimensions": [...], "limits": {...}, ...}` with a balanced inventory of every active
    core (no full content) and zero side effects -- rebalance (demote/archive/shorten/consolidate)
    and retry; this never needs a human decision. `core_review_after` defaults to 14 days out, max
    30; while any core is overdue, creating/promoting/enlarging a core or changing its review date
    is blocked (use review_core_memory first). Omitting `core_reason`/`core_exit_condition`/
    `core_review_after`/`detail_memory_ids` on an already-core update preserves existing values;
    supplying any of them on an effectively non-core write is rejected. `detail_memory_ids` (at
    most 3 UUIDs of existing shared, non-core memories whose title+UUID must appear in `content`)
    atomically maintains `elaborates_on` edges into this core -- `None` preserves the current
    declaration, `[]` clears it. `'#core'` in `tags` is a derived label the server auto-maintains
    from `is_core`; setting it directly is silently overridden.
    """
```

## 7. `search_memory`

Before (tools.py:493-517, the `@mcp.tool(description=...)` kwarg, quoted in full):

```python
    description="""Performs full-text keyword & dense vector hybrid search in long-term memory.
    FTS5 and BGE-prefixed dense retrieval form a weighted-RRF candidate pool; a
    deployment-configured cross-encoder supplies final ordering when enabled, with deterministic
    RRF fallback.

    Search by query, tags, context, memory type, or core status. `mode="strict"` resolves
    superseded matches and applies relevance abstention; `mode="history"` keeps matched history
    visible and labels superseded results; `mode="broad"` preserves ordinary retrieval. Each result
    item may include an optional `drift_flag` field, present only when a drift sweep flagged the
    memory; this field is advisory-only and never auto-corrected -- an agent seeing it should
    re-verify the citation itself, not blindly trust either the flag or the original claim.

    A query-based result item may also include `relevance_preview` (a string) plus
    `relevance_preview_meta` (an object with `auto_generated`, `extractive`, `query_specific`,
    and `complete` booleans, `complete` always `false`) -- a small set of verbatim excerpts
    pulled from that memory's own content, selected for relevance to this specific query. It
    is a hint for deciding whether to call `get_memory`, never a substitute for it: absence of
    a detail from the preview is never evidence that detail is absent from the memory itself.

    Explicit-ID retrieval is provided by the dedicated get_memory tool. Ranking stages cannot be
    changed per MCP call; benchmark controls for candidate channels, caps, lifecycle-family
    experiments, and diagnostics remain internal-only.

    """
```

Return type annotation before: `-> list | dict | str` (line 531). After: `-> list` (this tool's
external contract is always a bare list -- confirmed the `dict`-shaped
`return_diagnostics=True` branch of the underlying `orchestrator.py` is unreachable through this
tool's own exposed parameters, which never include `return_diagnostics`; a `str` return path was
not found anywhere in this tool's call chain). **`search_memory` is explicitly untouched by Spec
1** (deferred pagination fix, Spec 1 §16) -- this section changes only prose/type-annotation
accuracy, never claims a `has_more` field or an envelope shape this tool does not have.

After (opening paragraph per the locked reciprocal framing, memory `1446c55c`):

```python
    description="""Find individual memories matching a query or filters -- for locating a
    specific record, a duplicate check before storing, or historical/exploratory search. For
    task context assembled from matches AND their graph connections/lifecycle/conflicts in one
    call, use retrieve_context instead; avoid calling both with the same query, since
    retrieve_context already runs this search internally.

    query_keywords is the search text (omit it to browse by tags_filter/context_id/etc. alone).
    mode controls result filtering: "broad" (default) is ordinary hybrid ranking; "strict"
    resolves superseded matches and abstains (returns []) rather than return a weakly-relevant
    result; "history" keeps superseded results visible, labeled. tags_filter/context_id/
    memory_type_filter/is_core narrow the search; limit caps result count (default 5); cursor
    pages through a larger result set (this tool does not expose a `has_more`/total-count signal
    today -- an empty next page is your only current indicator that pagination is exhausted).

    Returns a bare list of memory dicts -- NOT the `{"status": ..., "data": ...}` envelope shape
    other SALTMDB tools use; each list item has its own fields (id, title, score, snippet, etc.)
    directly at the top level. An internal fault returns a one-item list `[{"error": "..."}]`
    rather than raising -- check for an `"id"` key's absence to detect this rare case, since it is
    the one property that reliably distinguishes it from every real result item.

    A result item may carry `relevance_preview`/`relevance_preview_meta` (a hint for whether to
    call get_memory, never a substitute for it -- absence of a detail from the preview is not
    evidence it's absent from the memory) or `drift_flag` (advisory only, re-verify the citation
    yourself rather than trusting either the flag or the original claim).

    Example: `search_memory(query_keywords="DNS incident", mode="broad", limit=5)`.
    """
```

## 8. `archive_memory`

Before (tools.py:553-577, docstring only, lines 555-559):

```python
    """Explicitly archives (retires) one or multiple long-term memories.

    Accepts entity_id as a single string ID OR a list of string IDs.

    """
```

Return type annotation before: `-> str | list` (line 554). After: `-> dict | list` -- **mode-
dependent**, per the asymmetry in §1's table: a single string (or omitted) `entity_id` returns the
envelope dict (post-Spec-1, §6.3); a list-valued `entity_id` (even a 1-item list) returns a bare
list of per-item `{"status": "success"/"error", ...}` dicts, entirely unchanged by Spec 1 (traced
directly to `duplicates.py`'s `bulk_archive_memory`, `-> list`, whose own external shape Spec 1
never touches).

After:

```python
    """Archives (retires) one or more memories -- the reversible-in-spirit-but-not-in-practice
    lifecycle action for a memory that's no longer worth surfacing in ordinary search, without
    deleting its history: an archived memory stays fully visible via get_memory/get_lineage, and
    is excluded from search_memory's normal ranking (but not from `mode="history"`).

    entity_id accepts a single ID (or unambiguous ID prefix) OR a list of IDs. **The return
    shape depends on which you pass**: a single ID (or omitted) returns
    `{"status": "ok", "data": {"id": ..., "message": "..."}, "warnings": [...]}`, with an
    `ALREADY_DONE`-coded warning (not a rejection) if it was already archived. A LIST of IDs --
    even a single-item list -- instead returns a bare list of `{"status": "success"/"error",
    "entity_id": ..., "result": ...}` dicts, one per requested ID, run all-or-nothing in one
    transaction (any single item's failure rolls back the whole batch, so a partial-success list
    is never returned -- a batch failure instead returns a single-item list,
    `[{"status": "error", "error": "..."}]`).

    A single-ID call's failure: `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR" |
    "NOT_FOUND" | "CONFLICT", "message": "...", "field": "entity_id" | "owner_id"}]}` --
    `NOT_FOUND` for an unresolvable ID, `CONFLICT` for an owner mismatch.

    Example, single: `archive_memory(entity_id="abc123")`. Example, bulk: `archive_memory
    (entity_id=["abc123", "def456"])` -- note the list wrapper changes the return shape as
    described above, not just the count of things archived.
    """
```

## 9. `manage_relation`

Before (tools.py:648-692, docstring only, lines 659-691):

```python
    """Stores one or multiple directional semantic relationship edges between memory nodes, or invalidates an existing edge (invalidate=True).

    `predicate` must be one of the 11 agent-selectable closed-vocabulary predicates
    (elaborates_on, related_to, resolves, depends_on, verifies, corrects, caused_by,
    derived_from, distinguishes_from, part_of, contradicts) -- see `list_predicates`. A drifted
    spelling is rejected with a `corrected_call` using the canonical name (and source_id/
    target_id swapped, when the drift reversed direction); `supersedes`/`consolidated_from`/
    `revises` are reserved and rejected, naming the lifecycle tool that creates them instead
    (`supersede_memory`/`consolidate_memories`/`revise_memory`); `similar_to` is legacy and
    read-only. This gate applies only to creating a new edge, never to `invalidate=True`.

    A governance gate rejects "strong" predicates (elaborates_on/resolves/supersedes) whose
    source/target chunk-embedding centroids fail a minimum similarity threshold
    (REJECT_LOW_RELATION_SIMILARITY), and rejects a contradictory predicate pair on the same
    directional edge (REJECT_CONTRADICTORY_PREDICATE), unless override_justification (a
    non-throwaway string explaining why this relation should proceed anyway) is supplied to
    force it through -- the override is atomically audited. For the bulk (`relations`) shape,
    put `override_justification` on each individual item that needs it.

    The bulk (`relations`) shape now supports invalidation, not just creation: set `invalidate:
    true` on an individual item dict to invalidate that specific edge instead of creating it (an
    optional per-item `invalid_at` sets the invalidation timestamp); an item that omits its own
    `invalidate` key falls back to the top-level `invalidate` parameter as a batch-wide default.
    Previously the bulk shape silently ignored `invalidate` entirely and always attempted to
    create every item regardless of intent, with no error and no signal that invalidation never
    happened -- this is now fixed. All-or-nothing batch semantics are unchanged: an invalidate
    target that cannot be found aborts the whole batch exactly like a failing create does.

    Core-memory governance: a NEW `elaborates_on` edge whose target is an active core memory is
    rejected (`REJECT_CORE_ELABORATES_ON`) -- only that core's own `detail_memory_ids`
    declaration (via `store_memory`/`consolidate_memories`) may create one. Re-submitting an
    edge that already exists stays an idempotent no-op regardless.

    """
```

Return type annotation before: `-> str | list | dict` (line 658). After: `-> dict | list` (drop
the stray `str`, which no path in this tool -- pre- or post-Spec-1 -- actually returns): single-
relation calls return the envelope dict (post-Spec-1, via `store_relation`/`invalidate_relation`,
§7.5/§7.6, or the tool's own already-correct predicate pre-flight `rejected()`); a `relations`
batch returns a bare list, unchanged by Spec 1 (per its own §7.7 note: "`bulk_store_relations`'s
own return type stays a bare `list`").

After (reordering per template -- ordinary single-edge use, batch note, then the governance/
migration detail pushed to the end):

```python
    """Creates a directional semantic edge between two memories (source_id -> predicate ->
    target_id), or invalidates one (invalidate=True). For a batch of edges in one call, use
    relations instead of source_id/target_id/predicate.

    predicate must be one of 11 agent-selectable values -- see list_predicates for the current
    list and each one's meaning; a drifted/non-canonical spelling is rejected with a
    corrected_call using the right one, so calling list_predicates first is optional, not
    required. Re-submitting an edge that already exists is an idempotent no-op, not an error.

    **Return shape depends on which form you use**: single-edge (source_id/target_id/predicate)
    returns `{"status": "ok", "data": {"relation_id": ..., "message": "..."}, "warnings":
    [...]}` -- an `ALREADY_DONE`-coded warning, not a rejection, for the idempotent-duplicate
    case. A `relations` BATCH instead returns a bare list of `{"status": "success"/"duplicate",
    "source", "target", "predicate", "action", "result"}` dicts, one per item, all-or-nothing
    (any item's failure aborts the whole batch; a batch failure returns a single-item list,
    `[{"status": "error", "error": "..."}]`, not a partial-success list).

    Rejections you may see (either form): `{"status": "rejected", "errors": [{"code":
    "RESERVED_PREDICATE" | "LEGACY_READONLY_PREDICATE" | "NONCANONICAL_PREDICATE" |
    "UNKNOWN_PREDICATE" | "REJECT_LOW_RELATION_SIMILARITY" | "REJECT_CONTRADICTORY_PREDICATE" |
    "REJECT_CORE_ELABORATES_ON", "message": "...", "field": "..."}], "corrected_call": {...} |
    None}` -- `corrected_call`, when present, is a ready-to-resubmit call with the fix already
    applied (e.g. the canonical predicate name, source/target swapped if the drift reversed
    direction). `RESERVED_PREDICATE` names the lifecycle tool that actually creates that edge
    (`supersede_memory`/`consolidate_memories`/`revise_memory` for `supersedes`/
    `consolidated_from`/`revises`).

    Example: `manage_relation(source_id="abc", target_id="def", predicate="depends_on")`.

    Two rejections are governance gates, not schema errors, and both accept
    override_justification (a non-throwaway explanation) to force the edge through anyway, on
    each affected item for a batch call: `REJECT_LOW_RELATION_SIMILARITY` (a "strong" predicate
    -- elaborates_on/resolves/supersedes -- whose source/target embeddings are too dissimilar)
    and `REJECT_CONTRADICTORY_PREDICATE` (a contradictory predicate pair already exists on this
    directional edge). `REJECT_CORE_ELABORATES_ON` has no override: a new `elaborates_on` edge
    targeting an active core memory can only be created via that core's own `detail_memory_ids`
    declaration (store_memory/consolidate_memories), never directly.
    """
```

## 10. `consolidate_memories`

Before (tools.py:777-818, docstring only, lines 794-818):

```python
    """Creates a canonical memory from two or more explicit parents.

    The parents are archived unchanged and linked with ``consolidated_from``. Semantic
    relations are never repointed; the response may include an optional orphaned-edge worklist
    whose items are safe, optional follow-up declarations. The new memory's identity is
    immutable after creation.

    A pairwise-cohesion gate rejects parent sets whose chunk-embedding centroids fail a minimum
    similarity threshold (REJECT_LOW_COHESION), unless override_justification (a non-throwaway
    string explaining why this merge should proceed anyway) is supplied to force it through --
    the override is baked into the committed content and atomically audited. For the bulk
    (`consolidations`) shape, put `override_justification` on each individual item that needs
    it, not at the top level -- it is never shared across items in the same batch.

    `context_id` is the batch-wide default for the bulk shape; an item's own `context_id` wins.

    Core-memory governance: `is_core` is NEVER inherited from parents. If any resolved parent is
    currently an active core (is_core=1, not archived) and `is_core` is omitted, the commit is
    rejected with an actionable error -- pass explicit `is_core=True` (with `core_reason`/
    `core_exit_condition`, each 20-500 chars, plus optionally `core_review_after`/
    `detail_memory_ids`) to keep the result core, or `is_core=False` to let it become an ordinary
    memory. The same capacity caps and detail-relation rules as `store_memory` apply. For the
    bulk shape, put every `core_*`/`detail_memory_ids` field on each individual item.

    """
```

Return type annotation before: `-> str | list | dict` (line 793). After: `-> dict | list` (the
`str` in the current annotation is stale independent of Spec 1 -- this domain function never
returned a bare string even before Spec 1; only Spec 1's §7.4 changes *how* the already-correct
dict shape is constructed, not whether one is returned). Single-set (`parent_ids`/`title`/
`content`) calls return the envelope dict (§7.4); a `consolidations` batch returns a bare list via
`bulk_commit_consolidation`, unchanged by Spec 1.

After:

```python
    """Merges two or more existing memories into one new canonical memory -- the parents are
    archived unchanged and linked via `consolidated_from`; the new memory's own identity is
    immutable afterward. For a batch of consolidations in one call, use `consolidations` instead
    of `parent_ids`/`title`/`content`.

    parent_ids (2+ existing memory IDs), title, content are required for a single consolidation.
    Semantic relations pointing at a parent are never auto-repointed onto the new memory; the
    response's `orphaned_relations`/`orphaned_edge_worklist` lists them as safe, optional
    follow-up `manage_relation` declarations you may choose to re-create.

    **Return shape depends on which form you use** (same pattern as `manage_relation`/
    `archive_memory`): a single consolidation returns `{"status": "ok", "data": {"entity_id":
    ..., "message": "...", "orphaned_relations": [...], "worklist_guidance": "..."}, "warnings":
    [...]}`. A `consolidations` BATCH instead returns a bare list of per-item result dicts,
    all-or-nothing (one item's failure aborts the whole batch).

    Rejections: `{"status": "rejected", "errors": [{"code": "REJECT_LOW_COHESION" | ..., ...}]}`
    -- `REJECT_LOW_COHESION` (the parent set's chunk-embedding centroids are too dissimilar to be
    one coherent memory) accepts `override_justification` (a non-throwaway explanation, baked
    into the committed content and audited) to force it through; for a batch, put
    `override_justification` on each item that needs it, never at the top level.

    Example: `consolidate_memories(parent_ids=["id1", "id2"], title="[Topic] Merged decision",
    content="...")`.

    `context_id` is the batch-wide default for the bulk shape; an item's own `context_id` wins.
    Core-memory governance: `is_core` is never inherited from parents -- if any resolved parent is
    an active core and `is_core` is omitted, the commit is rejected with an actionable error; pass
    `is_core=True` (with `core_reason`/`core_exit_condition`) to keep the result core, or
    `is_core=False` to let it become ordinary. Same capacity caps and `detail_memory_ids` rules as
    store_memory. For the bulk shape, put every `core_*`/`detail_memory_ids` field on each item.
    """
```

## 11. `revise_memory`

Before (tools.py:887-919, docstring only, lines 900-919):

```python
    """Repairs a deficient memory representation using a new immutable entity ID.

    ``entity_id`` is never mutated in place. The predecessor is archived byte-for-byte and the
    new entity links to it with ``revises``. An inactive target is a hard failure: inspect the
    reported successor before retrying. ``context_id``, ``scope``, and ``memory_type`` are
    inherited when omitted; provenance is assigned to the configured calling agent.

    ``repoint_relations`` (default False) opts in to server-side repointing: every active
    non-lifecycle edge touching the predecessor is invalidated and recreated onto the new entity,
    atomically with the revision, instead of being left for the caller to walk
    ``orphaned_semantic_edges`` and repoint by hand via ``manage_relation``. No predicate or
    direction filtering -- every edge found is repointed, since setting this flag is itself the
    caller's judgment call that this replacement is identity-preserving continuity, not a change
    that should leave any edge stale on purpose. Leave it False (the default) when the revision
    might invalidate what an existing edge asserted about the old content.

    Exactly one of ``content`` or ``content_file_path`` is required: pass ``content_file_path``
    (a local file SALTMDB reads server-side) instead of inlining a large revised body -- avoids
    round-tripping a large string through the calling agent's own context just to pass it here.
    """
```

Return type annotation before: `-> dict | str` (line 899). After: `-> dict` (this tool was
already envelope-adopted pre-Spec-1 on every reachable path; no bare-string return was found
anywhere in its chain, before or after Spec 1 -- the stray `str` in the current annotation is
tightened here as part of the same accuracy pass, not because Spec 1 changed this specific fact).

After (the key content change: `tags` joins the "inherited when omitted" list, per Spec 1 §4.3/
§6.1/§6.2, and the tool now states explicitly that a validation failure is a clean returned value,
never a raised exception -- directly reproducing the exact failure mode Codex's original audit
found):

```python
    """Corrects a memory whose *representation* was flawed (wrong wording, an error you made
    recording it, a wrong unit) -- content changes, but the underlying fact/decision does not. Use
    supersede_memory instead when the real-world fact/decision itself has changed;
    update_memory_metadata for a metadata-only edit needing no content restatement.

    entity_id (the active memory to revise), title, and exactly one of content/content_file_path
    are required. tags, context_id, scope, and memory_type are all optional and INHERITED from
    the predecessor when omitted -- pass an explicit value only to change it, never to keep the
    old one. entity_id is never mutated in place: the predecessor is archived byte-for-byte and
    the new entity links back to it via `revises`.

    Returns `{"status": "ok", "data": {"old_id": ..., "new_id": ..., "inherited": {...},
    "changed": {...}, ...}, "warnings": [...]}` -- `inherited` lists every field you omitted
    (including `tags`, if omitted) with the value actually carried forward; `changed` lists every
    field you explicitly supplied. A validation problem -- a missing required field, or targeting
    an already-inactive `entity_id` -- returns a clean `{"status": "rejected", "errors": [{...}],
    ...}` value; it never raises an exception across the tool boundary. An inactive target names
    its known successor in the rejection -- inspect it (get_memory) before retrying, rather than
    reissuing the same call.

    Example: `revise_memory(entity_id="abc123", title="[Worker] Request timeout", content="The
    worker request timeout is 30 seconds.", reason="Corrected the unit from milliseconds.")` --
    `tags`/`context_id`/`scope`/`memory_type` all omitted here are inherited from `abc123`
    unchanged.

    `repoint_relations` (default False) opts in to server-side repointing: every active
    non-lifecycle edge touching the predecessor is invalidated and recreated onto the new entity
    atomically, instead of leaving `orphaned_semantic_edges` for you to walk and repoint by hand
    via manage_relation. No predicate/direction filtering -- setting this flag is itself your
    judgment that the replacement is identity-preserving continuity, not a change that should
    leave any edge stale on purpose. Leave it False when the revision might invalidate what an
    existing edge asserted about the old content.
    """
```

## 12. `supersede_memory`

Before (tools.py:941-976, docstring only, lines 954-976):

```python
    """Replaces valid knowledge with newer knowledge using a new immutable entity ID.

    The predecessor remains byte-identical and is linked with ``supersedes``. An inactive target
    is never silently redirected; the error reports known active successors and lineage. Optional
    administrative fields are inherited unless explicitly supplied.

    ``repoint_relations`` (default False) opts in to server-side repointing: every active
    non-lifecycle edge touching the predecessor (``manage_relation``'s closed-vocabulary
    predicates, both directions) is invalidated and an equivalent edge recreated onto the new
    entity, atomically with the supersession -- no separate ``orphaned_semantic_edges`` walk plus
    manual ``manage_relation`` calls needed afterward. No predicate or direction filtering: every
    edge found is repointed unconditionally, since passing this flag is itself the caller's
    judgment that the supersession is additive/index-like continuity (e.g. a growing tracker
    document) rather than a correction that might leave some edge intentionally stale against the
    old content. The response's ``semantic_relations_repointed`` echoes this flag, and
    ``repointed_relations`` lists each old/new relation id pair actually rewritten.

    Exactly one of ``content`` or ``content_file_path`` is required: pass ``content_file_path``
    (a local file SALTMDB reads server-side) instead of inlining a large new body -- avoids
    round-tripping a large string through the calling agent's own context (and the response's own
    size limit) just to pass it here. Pairs naturally with a large document that was already
    edited on disk, such as a wayfinder map's growing body.
    """
```

Return type annotation before: `-> dict | str` (line 953). After: `-> dict` (identical reasoning
to §11 -- `revise_memory`/`supersede_memory` share the same `_replacement_payload`/
`_dispatch_replacement`/`_replacement_operation` chain end to end).

After (same `tags`-inherits-when-omitted fix as §11, applied to this tool's own wording; kept
distinct from `revise_memory` per the tool-choice paragraph each needs):

```python
    """Replaces knowledge that was once valid with newer knowledge -- the real-world fact/
    decision itself has changed, not just how it was worded. Use revise_memory instead when
    you're only correcting a representational error (wrong wording/unit) in an otherwise-still-
    true memory; update_memory_metadata for a metadata-only edit.

    entity_id (the active memory to supersede), title, and exactly one of content/
    content_file_path are required. tags, context_id, scope, and memory_type are all optional and
    INHERITED from the predecessor when omitted. The predecessor remains byte-identical and is
    linked via `supersedes`; an inactive target is never silently redirected.

    Returns `{"status": "ok", "data": {"old_id": ..., "new_id": ..., "inherited": {...},
    "changed": {...}, "semantic_relations_repointed": bool, "repointed_relations": [...], ...},
    "warnings": [...]}` on success, `{"status": "rejected", "errors": [{...}]}` -- naming known
    active successors and lineage -- when the target is already inactive. Never raises an
    exception across the tool boundary for a validation problem.

    Example: `supersede_memory(entity_id="abc123", title="[Roadmap] Q3 priorities", content="...
    (updated priorities)", reason="Priorities changed after the Q2 retro.")`.

    `repoint_relations` (default False) opts in to server-side repointing: every active
    non-lifecycle edge touching the predecessor is invalidated and an equivalent edge recreated
    onto the new entity atomically -- no separate `orphaned_semantic_edges` walk plus manual
    manage_relation calls needed. Setting this flag is your judgment that the supersession is
    additive/index-like continuity (e.g. a growing tracker document), not a correction that
    should leave some edge intentionally stale against the old content; `repointed_relations`
    lists each old/new relation id pair actually rewritten. Pairs naturally with a large document
    already edited on disk via `content_file_path`, such as a wayfinder map's growing body.
    """
```

## 13. `get_memory`

Before (tools.py:998-1007, docstring only, lines 1000-1005):

```python
    """Retrieves one memory by full ID or an unambiguous ID prefix.

    Explicit retrieval includes archived memories and returns the memory's status and lineage;
    an archived ID is never silently redirected to a successor.

    """
```

Return type annotation: `-> dict`, unchanged and already accurate (already envelope-adopted
pre-Spec-1, not touched by Spec 1).

After (this tool's mechanics were never wrong; the only real gap is that its own return-shape
contract, and its explicit contrast with `search_memory`, were never stated):

```python
    """Retrieves ONE memory in full, by exact ID or an unambiguous ID prefix -- when you already
    know (or can uniquely identify) which memory you want. Use search_memory instead to find a
    memory by content/tags/context when you don't already have its ID.

    Explicit retrieval includes archived memories (an archived ID is never silently redirected to
    a successor) and always returns full content, status, and lineage -- unlike search_memory's
    result items, which carry only a snippet/preview.

    Returns `{"status": "ok", "data": {"id", "title", "content", "tags", "status", "lineage",
    ...}, "warnings": [...]}`. `{"status": "rejected", "errors": [{"code": "UNKNOWN_ENTITY_ID" |
    "AMBIGUOUS_ID_PREFIX", "message": "...", "field": "entity_id"}]}` when the ID doesn't resolve
    to exactly one memory -- `AMBIGUOUS_ID_PREFIX` names the matching candidates so you can
    disambiguate with a longer prefix.

    Example: `get_memory(entity_id="a1b2c3")`.
    """
```

## 14. `inspect_memory`

Before (tools.py:1010-1019, docstring only, lines 1012-1015):

```python
    """Lighter-weight sibling to get_memory: same field set minus `content`, replaced by a
    `snippet` (first ~3 non-heading lines, truncated). Lineage is included. Use this when you
    want to check other fields or aren't sure this is the memory you want, without pulling the
    full body -- get_memory remains the only path to full content."""
```

Return type annotation: `-> dict`, unchanged and already accurate.

After:

```python
    """Lighter-weight sibling to get_memory: identical field set minus `content`, replaced by a
    short `snippet` (first ~3 non-heading lines, truncated). Use this when you want to confirm a
    memory's identity/metadata/lineage, or aren't yet sure this is the one you want, without
    pulling the full body -- get_memory is the only path to full content once you're sure.

    Returns `{"status": "ok", "data": {"id", "title", "snippet", "tags", "status", "lineage",
    ...}, "warnings": [...]}`; `{"status": "rejected", "errors": [{"code": "UNKNOWN_ENTITY_ID" |
    "AMBIGUOUS_ID_PREFIX", ...}]}` on the same conditions as get_memory.

    Example: `inspect_memory(entity_id="a1b2c3")` to confirm which memory a search hit actually
    is before deciding whether to pull it in full.
    """
```

## 15. `get_lineage`

Before (tools.py:1022-1043, docstring only, lines 1028-1033):

```python
    """Traverses memory lineage in either direction.

    ``ancestors`` shows where an entity came from; ``descendants`` shows what it became.
    Archived nodes remain visible so historical provenance is never hidden.

    """
```

Return type annotation: `-> dict`, unchanged (already accurate both before and after Spec 1 --
Spec 1 §7.2 changes what's *inside* the dict, wrapping it in the envelope, not the top-level
type).

After (the real content change: the traversal data now lives under `data`, not top-level, and
failures are now the standard envelope shape rather than a bare `{"error": ...}`):

```python
    """Traverses a memory's revision/supersession/consolidation lineage in either direction --
    `ancestors` shows where it came from, `descendants` shows what it became. Archived nodes
    remain fully visible, so historical provenance is never hidden by an archive.

    direction: "ancestors" (default) or "descendants". max_depth caps traversal hops (default 5).

    Returns `{"status": "ok", "data": {"entity_id": ..., "direction": ..., "root": {...},
    "nodes": [...], "edges": [...], "total": ..., "total_nodes": ..., "graph_exhausted": ...,
    "point_in_time": ..., "max_depth": ...}, "warnings": [...]}` -- `nodes` holds every node
    found in the requested `direction` (the key is always `nodes`, never renamed to
    `ancestors`/`descendants` -- `direction` itself is the only place those two words appear in
    the response); `edges` lists the connecting lineage relations, each with
    `source_id`/`target_id`/`predicate`/`depth`/`source_title`/`source_status`/`target_title`/
    `target_status`. `graph_exhausted` is `true` when the traversal reached every real edge
    within `max_depth` (nothing was cut off). `{"status": "rejected", "errors": [{"code":
    "VALIDATION_ERROR", "message": "...", "field": "entity_id" | "direction" | "max_depth"}]}`
    for a missing/malformed parameter.

    Example: `get_lineage(entity_id="a1b2c3", direction="descendants")` to see every memory that
    eventually replaced this one.
    """
```

## 16. `get_related_memories`

Before (tools.py:1046-1075, docstring only, lines 1053-1064):

```python
    """Traverses semantic relations from one memory for up to ``max_depth`` hops.

    direction="both" (default) walks outbound and inbound edges independently and merges the
    results -- an entity that is only ever a relation's *target* now surfaces those relations
    too, instead of always reporting zero. This is a union of two single-direction traversals,
    not a true mixed-direction graph walk: pass direction="outbound" for the original
    downstream-only behavior, or direction="inbound" for upstream-only.

    Set include_inspect=True to inline the inspect_memory fields for every returned node, without
    full content or lineage.

    """
```

Return type annotation: `-> dict`, unchanged (still a dict on every path, pre- and post-Spec-1).

After (the real content change: no more `dependencies` key -- only `related_memories` -- and the
whole thing is now envelope-wrapped):

```python
    """Traverses semantic relations (manage_relation edges, not lineage) from one memory for up
    to `max_depth` hops. Use get_lineage instead for revision/supersession/consolidation history;
    this tool is for the separate graph of relations you create via manage_relation.

    direction="both" (default) walks outbound and inbound edges independently and merges the
    results (a union of two single-direction traversals, not a true mixed-direction walk) --
    pass "outbound" or "inbound" to walk only one. include_inspect=True inlines each returned
    node's inspect_memory fields (no full content/lineage).

    Returns `{"status": "ok", "data": {"root": {...}, "related_memories": [...],
    "total_related_found": ..., "max_depth": ...}, "warnings": [...]}` -- `related_memories` is
    the one and only node-list key in the response; no other key duplicates it under a different
    name. `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR", "message": "..."}]}`
    for an unresolvable `entity_id`.

    Example: `get_related_memories(entity_id="a1b2c3", direction="outbound", max_depth=2)`.
    """
```

## 17. `retrieve_context`

Before (tools.py:1078-1126, docstring only, lines 1085-1115):

```python
    """Assembles graph-aware, budget-bounded local context for a query in one call: primary search
    hits, one-hop predicate-allowlisted graph expansion, contradiction/conflict-set surfacing,
    lifecycle/supersession history, and deterministic token-budget packing -- everything
    search_memory + get_related_memories + get_lineage would otherwise require composing by hand.

    Returns one memories[] array (each item tagged inclusion: "primary"|"expansion"|
    "conflict_only", plus a retrieval_provenance explaining how it entered the result), the
    in-network edges directly connecting primary hits, a lineage map for every surfaced head with a
    known supersession chain (contradiction-flagged where relevant), conflict_sets for any
    unresolved contradiction touching this result (a lifecycle-resolved one surfaces via lineage,
    never here), and metadata.fan_out/metadata.budget truncation accounting. A primary-hit item may
    also include relevance_preview/relevance_preview_meta -- the same query-focused extractive
    preview search_memory returns, carried through unchanged (see search_memory's own description
    for the field's exact semantics and budget-degradation contract); expansion/conflict_only items
    never carry it, since they were never matched against the query directly.

    limit controls how many primary search hits seed expansion (default 5, matching search_memory's
    own default). budget_tokens caps the total token payload of memories[] (server default and hard
    ceiling apply if omitted). All other traversal behavior -- which relation predicates expand
    context, how far, and in which direction -- is fixed for this tool and not caller-configurable.
    strategy selects the retrieval mode: "local" (the default) is the existing bounded one-hop-plus
    expansion described above. "global" instead seeds from whichever leaf community-detection
    clusters (see Milestone C) are nearest the query by embedding similarity, synthesizing a
    representative-plus-ranked-members result per seeded community for whole-topic breadth rather
    than one-hop-local depth -- it never runs the primary-search/expansion/conflict/lineage pipeline
    above at all, so edges/lineage/conflict_sets are always empty and memories[] items instead carry
    inclusion "community_representative"|"community_member" with a different retrieval_provenance
    shape (community_id plus a similarity score) and metadata carries a new metadata.community
    namespace in place of metadata.fan_out.

    """
```

Return type annotation: `-> dict`, unchanged. **Content change is the important part**: this
tool's success path is the one place among all 19 where the return is deliberately NOT the
`{"status", "data"}` envelope -- Spec 1 explicitly leaves `assemble_retrieve_context` untouched
(§16 of Spec 1: "any change to the graph-aware-context-retrieval feature ... beyond
`_dispatch_retrieve_context`'s own validation-mechanism change"). Only the *failure* path (an
invalid `query`) changed, from a raised exception to a clean `rejected()` return.

After (per the locked reciprocal opening, memory `1446c55c`, plus the success/failure shape
asymmetry stated explicitly since it's the one genuine exception in this whole document):

```python
    """Assemble a budget-bounded memory context for a task in one call: primary search hits, one-
    hop graph expansion, contradiction surfacing, lifecycle history, and token-budget packing --
    everything search_memory + get_related_memories + get_lineage would otherwise require
    composing by hand. For precise filters or locating one specific record, use search_memory
    instead; avoid calling both with the same query, since this tool already searches internally.

    query is required. limit caps how many primary search hits seed expansion (default 5).
    budget_tokens caps the total token payload of `memories[]` (server default/ceiling apply if
    omitted). strategy: "local" (default) is one-hop-plus-expansion around direct search hits;
    "global" instead seeds from the nearest community-detection clusters for whole-topic breadth,
    trading graph-local depth for topic coverage.

    **Return shape note**: on success this tool does NOT use the `{"status": "ok", "data": ...}`
    envelope other SALTMDB tools use -- it returns its own bespoke top-level shape directly:
    `{"query": ..., "memories": [...], "edges": [...], "lineage": {...}, "conflict_sets": [...],
    "metadata": {...}}`. Only a caller-input problem (a missing/non-string `query`) returns the
    standard `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR", "field": "query"}]}`
    envelope instead -- check for a top-level `"status"` key to distinguish the two: its presence
    means rejection, its absence means the bespoke success shape above.

    Each `memories[]` item is tagged `inclusion`: `"primary"` (a direct search hit),
    `"expansion"` (reached via one-hop graph traversal), or `"conflict_only"` (force-included
    because it contradicts something else in the result) for `strategy="local"`; or
    `"community_representative"`/`"community_member"` for `strategy="global"` (which never
    populates `edges`/`lineage`/`conflict_sets` at all -- always `[]`/`{}` -- and uses
    `metadata.community` in place of `metadata.fan_out`). Every item's own `retrieval_provenance`
    explains how it entered the result; a primary-hit item may also carry
    `relevance_preview`/`relevance_preview_meta` (identical semantics to search_memory's own
    field of the same name).

    Example: `retrieve_context(query="DNS incident root cause", limit=5, budget_tokens=4000)`.
    """
```

## 18. `get_events`

Before (tools.py:1129-1174, docstring only, lines 1141-1160):

```python
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
```

Return type annotation: `-> list`, unchanged and accurate. **`get_events` is explicitly untouched
by Spec 1** -- not in its scope at all. This section's only change is adding the explicit
"reciprocal" note to `log_event` (already added in §2) and stating this tool's own bare-list
contract plainly, matching the same honesty standard applied to `search_memory` in §7.

After (largely the existing text, already close to the template; the added material is the
explicit return-shape statement and the reciprocal pointer back to `log_event`):

```python
    """Reads back events from the append-only events ledger -- for multi-agent coordination
    (what did another agent just log/decide) and wrap-up/session-review. Use log_event to write
    an event; this tool never writes.

    context_id is the headline filter (every event logged under a shared thread handle,
    survivable across a power cut). agent_id filters to one agent's events -- this tool has no
    notion of "my own events" the way log_event has a bound owner; it reads across the whole
    ledger, narrowed by whichever filters you supply. agent_session_id filters to one adapter
    process session. order: "newest_first" (default) or "oldest_first" (for chronological
    synthesis).

    Returns a bare list of event dicts (`{"id", "timestamp", "agent_id", "type", "content",
    ...}`) -- NOT the `{"status": ..., "data": ...}` envelope other SALTMDB tools use; an empty
    result is just `[]`, not distinguishable from an error by shape alone (there is no current
    error-sentinel convention documented for this tool to rely on).

    Content over 1000 characters is truncated ("[TRUNCATED]" suffix) by default. Two ways to get
    full text back, mirroring get_memory's list-then-fetch-by-ID pattern: event_id (exact match
    on the primary key) always returns full content regardless of full_content; full_content=True
    disables truncation for every row a broader query matches -- use it once your other filters
    already bound the result set to something you know is safe to receive in full.

    Example: `get_events(context_id="my-session-thread", order="oldest_first")` to replay a
    thread chronologically.
    """
```

## 19. `review_core_memory`

Before (tools.py:1177-1206, docstring only, lines 1184-1195):

```python
    """Reviews an active core memory: retain (extend its next review date), demote (turn it back
    into an ordinary searchable memory), or archive (retire it) -- a direct, synchronous
    operation, never a request/queue/event.

    The configured owner identifies the reviewing agent; it need not match the entity's own
    owner and never transfers ownership. `review_rationale` (20-1,000 chars) is stored for provenance but never
    injected into the bootstrap digest. `retain` requires an absolute `core_review_after`
    timestamp in the future and no more than 30 days out (omit it to default to 14 days from
    now); `demote`/`archive` must not supply `core_review_after`. Meaningful CONTENT revision is
    still a `store_memory` update -- this tool changes lifecycle state only. Repeating `demote`/
    `archive` on an already-non-core/already-archived memory is a no-op, not an error; `retain`
    against a non-core or archived memory is rejected.
    """
```

Return type annotation before: `-> str` (line 1183). After: `-> dict` (envelope shape post-Spec-1,
§12.1).

After:

```python
    """Reviews an active core memory: retain (extend its review deadline), demote (turn it back
    into an ordinary searchable memory), or archive (retire it) -- a direct, synchronous
    operation. Use store_memory instead for a meaningful CONTENT revision; this tool only ever
    changes lifecycle/review state.

    entity_id and outcome ("retain"/"demote"/"archive") are required. review_rationale (20-1,000
    chars) is stored for provenance, never injected into the bootstrap digest. `retain` requires
    an absolute core_review_after in the future, at most 30 days out (omit for the 14-day
    default); demote/archive must not supply core_review_after. The configured calling agent need
    not match the entity's own owner, and reviewing never transfers ownership.

    Returns `{"status": "ok", "data": {"id": ..., "message": "..."}, "warnings": [...]}` --
    demote/archive on an already-non-core/already-archived memory is an `ALREADY_DONE`-coded
    warning, not a rejection. `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR" |
    "NOT_FOUND" | "CONFLICT", "message": "..."}]}` -- `VALIDATION_ERROR` for a malformed
    outcome/timestamp, `NOT_FOUND` for an unresolvable entity_id, `CONFLICT` for a `retain`
    against a memory that isn't currently an active core (already demoted or archived).

    Example: `review_core_memory(entity_id="abc123", outcome="retain",
    review_rationale="Still an active hazard; extending review window.")`.
    """
```

## 20. `update_memory_metadata`

Before (tools.py:1209-1225, docstring only, lines 1211-1218):

```python
    """Shallow-merges `metadata` into an existing memory without requiring title/content/tags
    to be restated (unlike store_memory(entity_id=..., metadata=...), which needs those fields
    byte-identical for a metadata-only edit). Submitted keys overwrite/add; keys not mentioned
    are preserved untouched. There is no key-deletion sentinel -- submit a key with value null
    to mark it cleared (the key itself stays present with a null value, it is not stripped).
    Works uniformly on core and non-core memories; core lifecycle fields (outcome,
    core_review_after, review_rationale) are governed exclusively by review_core_memory, not
    this tool."""
```

Return type annotation before: `-> str` (line 1210). After: `-> dict` (envelope shape post-Spec-1,
§6.4).

After (correcting one stale claim in passing: the current docstring says
`store_memory(entity_id=..., metadata=...)` "needs those fields byte-identical" -- confirmed
against `store_memory`'s own current docstring/behavior in §6 above, `entity_id`-targeted updates
are already metadata-only and do NOT require restating title/content; the accurate contrast is
that this tool needs no `content_file_path`/duplicate-check/quality-gate machinery at all, not
that `store_memory`'s own entity_id path is harder to use than it actually is):

```python
    """Shallow-merges `metadata` into an existing memory -- the lightest-weight way to update
    metadata only, with no quality gate, duplicate check, or content-hash machinery involved (an
    alternative to store_memory(entity_id=..., metadata=...), which also supports a metadata-only
    update but carries store_memory's own full validation path).

    entity_id and metadata (a dict) are required. Submitted keys overwrite/add; keys not
    mentioned are preserved untouched. There is no key-deletion sentinel -- submit a key with
    value null to mark it cleared (the key stays present with a null value, it is not stripped).
    Works uniformly on core and non-core memories; core lifecycle fields (outcome,
    core_review_after, review_rationale) are governed exclusively by review_core_memory, not this
    tool.

    Returns `{"status": "ok", "data": {"id": ..., "message": "..."}, "warnings": [...]}` --
    including when the patch was empty (still success, not a warning: nothing was rejected).
    `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR" | "AMBIGUOUS_ID_PREFIX" |
    "NOT_FOUND", "message": "...", "field": "entity_id" | "metadata"}]}` for a malformed
    parameter or an unresolvable/ambiguous entity_id.

    Example: `update_memory_metadata(entity_id="abc123", metadata={"project_id": "wf-42"})`.
    """
```

## 21. Out of scope

- **Any behavior/schema/parameter change.** No parameter is added, removed, renamed, retyped
  (beyond the return-type-annotation corrections named per-tool above, which describe existing
  behavior, they don't create any), or defaulted differently. `search_memory`/`search_tags`
  gaining a `has_more` field, `get_events`/`search_memory` gaining the standard envelope, or any
  other capability neither this spec nor Spec 1 actually ships -- none of that is described here
  as present, and none of it is added.
- **Relation-direction/predicate labeling on `get_related_memories`'s nodes** -- explicitly
  rejected in the locked grilling record (Q9); §16's rewrite describes the shape exactly as Spec 1
  ships it, no more.
- **The trace-layer/`search_trace` tool concept** from Codex's 2026-09-15 session -- discussion
  only, never approved, not part of either spec.
- **Any change to `assemble_retrieve_context`'s own algorithm or the Milestone A-E graph-aware-
  retrieval feature** -- §17 documents `retrieve_context`'s existing behavior accurately (including
  the deliberate non-envelope success shape); it changes no code.
- **A broader pass over `README.md`/`AGENT_GUIDE.md`** beyond what Spec 1 Amendment 1 already
  fixed (the five now-corrected capacity-gate references) -- those two files describe the same
  tools this spec's docstrings describe, and likely have their own staleness/duplication worth a
  dedicated pass, but that pass is unscoped, unreviewed, and explicitly deferred to a future spec,
  not silently bundled into this one.
- **The `saltmdb-usage` skill's own content** -- unrelated surface; this spec is only about what a
  skill-free agent can learn from the MCP tool descriptions/schemas themselves.
- **A full type-annotation audit of every non-public helper function in `tools.py`** (e.g.
  `_resolve_content`, `_predicate_disposition_error`) -- only the 19 `@mcp.tool()`-decorated
  functions' own signatures were checked and are in scope; an internal helper's type hints are not
  part of the exposed MCP schema an agent reads.
- **Renaming or restructuring any existing error code** (`MISSING_TITLE`, `INVALID_TAGS`,
  `REJECT_LOW_RELATION_SIMILARITY`, etc.) -- this spec only documents codes exactly as Spec 1
  and the pre-existing codebase already define them; no code's name or meaning changes here.
- **Creating any new test file.** Verification is the static/behavioral checks in §22 below,
  applied against the existing test suite plus the one throwaway acceptance script -- matching
  Spec 1's own precedent of not adding permanent test files for a contract/description spec.

## 22. Acceptance

**Static review** (a human/reviewer checklist, not fully automatable, but each item below has a
concrete check where one exists):

1. Every one of the 19 `@mcp.tool()` docstrings in `mcp/tools.py`, read in isolation with no other
   context, states: what the tool is for, which closely-related tool to use instead when one
   exists (`search_memory`/`retrieve_context`, `revise_memory`/`supersede_memory`/
   `update_memory_metadata`/`store_memory`, `log_event`/`get_events`), required vs. optional
   inputs, the exact return shape on success and on failure (including any mode-dependent
   asymmetry), and at least one concrete example call.
2. No docstring claims a `has_more`/pagination-cursor-count field on `search_memory` or
   `search_tags`, or the standard `{"status", "data"}` envelope for `search_memory`,
   `get_events`, or `retrieve_context`'s own success path -- verify via
   `rg -n 'has_more' src/saltmdb/mcp/tools.py` (must show zero matches) and manual read of the
   three named tools' docstrings against §7/§17/§18 above.
3. `get_related_memories`'s docstring does not mention a `dependencies` key --
   `rg -n 'dependencies' src/saltmdb/mcp/tools.py` (excluding this document itself, obviously)
   must show no match inside `get_related_memories`'s own docstring.
4. Every return type annotation named as changed in §2-§20 above matches the actual signature in
   the implemented tree -- `rg -n "^\s*\) -> " src/saltmdb/mcp/tools.py` and manually cross-check
   each of the 19 against the table in §1 and the per-tool sections.

**Behavioral regression** (a concrete script, run against a real in-memory DB with no skill
loaded -- reproducing Codex's original 2026-09-15 probe as a check that both specs together
actually closed the gap it found):

```bash
PYTHONPATH=src uv run python -c "
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, relation_service
import sqlite3
conn = sqlite3.connect(':memory:')
init_db(conn)

# 1. revise_memory with tags omitted succeeds and inherits (the original confirmed bug).
r1 = memory_service.store_memory(title='T', content='C', tags=['x'], owner_id='o', db_connection=conn)
assert r1['status'] == 'ok', r1
eid = r1['data']['id']
r2 = memory_service.revise_memory(entity_id=eid, title='T2', content='C2', reason='fix', db_connection=conn)
assert r2['status'] == 'ok', r2
assert r2['data']['inherited'].get('tags') == ['x'], r2
new_id = r2['data']['new_id']

# 2. revise_memory against an already-inactive target: a clean rejected() naming the successor,
#    never a raised exception.
try:
    r3 = memory_service.revise_memory(entity_id=eid, title='T3', content='C3', reason='fix', db_connection=conn)
    assert r3['status'] == 'rejected', r3
except Exception as e:
    raise AssertionError(f'revise_memory raised instead of returning rejected(): {e}')

# 3. list_predicates returns real disposition data.
r4 = relation_service.list_predicates(query='depends_on', db_connection=conn)
assert r4['status'] == 'ok', r4
assert any(p['name'] == 'depends_on' and p.get('disposition') == 'selectable' for p in r4['data']), r4

# 4. get_related_memories has no duplicate 'dependencies' key.
relation_service.store_relation(source_id=new_id, target_id=eid, predicate='related_to', owner_id='o', db_connection=conn)
r5 = relation_service.get_related_memories(entity_id=new_id, db_connection=conn)
assert r5['status'] == 'ok', r5
assert 'dependencies' not in r5['data'], r5
assert 'related_memories' in r5['data'], r5

print('OK')
"
```

Must print `OK` with no assertion error and no uncaught exception -- reproducing, and confirming
fixed, all four concrete friction points Codex's original skill-free audit found.

## Amendment 1

Independent verification pass (2026-09-20, coordinator plus a dedicated verification fork),
performed after Spec 1 actually shipped and merged (`develop@e8ce355`) -- re-checking every one
of this document's 19 per-tool factual claims directly against the real shipped source, not
against Spec 1's own locked prose (which this document was originally drafted against, before
Spec 1's Amendment 2 landed). No OMP work was in flight -- this is a pre-implementation
correction, not an adjudication of an in-progress `BLOCKED` report. 17 of 19 tools were confirmed
a clean match; two drifts were found, both traceable to Spec 1's Amendment 2 (the `get_lineage`
internal-reader fanout fix) landing after this document was originally drafted:

1. **§15 `get_lineage`'s claimed return shape was wrong, not just imprecise.** The original text
   claimed `data` contains `"ancestors": [...] | "descendants": [...]` depending on `direction`.
   Confirmed directly against `relation_service.py`'s `_get_lineage_raw` (:940-1091, feeding
   `get_lineage`'s envelope wrapper at :1093-1118): the actual key is always `nodes` regardless of
   `direction` (`direction` only ever appears as its own field, echoing back the parameter),
   alongside `edges`, `total`, `total_nodes`, `graph_exhausted`, `point_in_time`, and `max_depth`
   -- none of which the original text mentioned at all. An agent following the original docstring
   as written would call `result["data"]["ancestors"]` and get a `KeyError` on every single call,
   the exact class of failure this whole two-spec effort exists to prevent. **Fixed**: §15's
   `Returns` line now states the real key set (`entity_id`, `direction`, `root`, `nodes`, `edges`,
   `total`, `total_nodes`, `graph_exhausted`, `point_in_time`, `max_depth`), states plainly that
   `nodes` is the one and only node-list key regardless of `direction`, and names `edges`' own
   per-item fields.

2. **§19 `review_core_memory`'s enumerated failure codes were incomplete.** The original text
   listed only `VALIDATION_ERROR`/`NOT_FOUND` for every rejection. Confirmed directly against
   `core_governance_service.py::review_core_memory` (:1109-1119): a `retain` against a memory that
   isn't currently an active core (already demoted or archived) returns `error_codes.CONFLICT`,
   not `VALIDATION_ERROR` or `NOT_FOUND` -- a real, distinct failure mode the original enumeration
   silently dropped. **Fixed**: §19's `Returns` line now lists `CONFLICT` alongside the other two
   codes, with each code's own triggering condition stated explicitly rather than left implicit.

**Not touched, checked and confirmed still correct**: the §1 precondition table's `get_lineage`
row already used generic "node data" wording rather than naming `ancestors`/`descendants` as
response keys, so it required no correction. The §22 acceptance script's behavioral-regression
check does not call `get_lineage` or `review_core_memory` at all, so no acceptance-script change
was needed either.

**Amendment pre-lock re-check**: independently re-ran both source reads above against the current
tree a second time immediately before writing this amendment (not trusting the verification
fork's report alone) -- both confirmed byte-for-byte accurate. Full manual re-read of §15 and §19
in isolation, as an agent with no other context would read them, confirms neither now claims
anything the real source doesn't support. Status remains **LOCKED**.
