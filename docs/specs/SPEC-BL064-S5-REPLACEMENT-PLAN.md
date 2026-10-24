# SPEC-BL064-S5-REPLACEMENT-PLAN

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 5 (`_replacement_prepare` / `_replacement_fields` and the untyped replacement `context` dict). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `90df320` or later; `./verify` exits 0 at `90df320` (3638 passed, 10 skipped, 391 subtests passed; the BL-083 teardown flake can fail two tests in a run, rerun once). Targeted baseline: `.venv/bin/python -m pytest tests/test_replace_memory_v2.py tests/test_lifecycle_replacements.py tests/test_lifecycle_replacements_characterization.py tests/test_mcp_replace_tools_v2.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `replace_memory(operation, request)` (public; direct tests in `tests/test_replace_memory_v2.py`), `revise_memory` and `supersede_memory` (service) and the MCP tools above them.
- Scope (may edit): `src/saltmdb/domain/services/memory_service/lifecycle.py` (the functions `_replacement_content`, `_replacement_fields`, `_replacement_prepare`, `_replacement_insert_core_tags`, `_replacement_enqueue_jobs`, `_replacement_insert_entity`, `_replacement_write`, `_replacement_response`, `replace_memory` and `_replacement_execute`, lines 358 to 612 and 738 to 998, plus the dataclass imports), `tests/test_replace_memory_v2.py` (new characterization tests only).
- Does not touch: `ReplacementRequest` and the signatures of `replace_memory`, `revise_memory`, `supersede_memory`, `_replacement_error`, `_replacement_target`, `_replacement_snapshot`, `_replacement_locked_source`, `_replacement_repoint_edges` (the last two names are patched or wrapped by existing tests: `tests/test_replace_memory_v2.py` 606 patches `_replacement_repoint_edges`, `tests/test_lifecycle_replacements_characterization.py` 164 wraps `_replacement_snapshot`), `daemon/dispatch.py`, any other module, any SQL text.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

One revise/supersede builds a plain `dict` named `context` in `_replacement_content`, `_replacement_fields`, `_replacement_prepare` and `replace_memory`, then mutates it in `_replacement_write` (`new_id`, `core_state`) and reads it, by string key, in seven functions (22 distinct keys, 60 reads). Nothing declares which key exists at which stage, so a misspelt key is a `KeyError` at run time. The signatures reflect the same problem: `_replacement_prepare` takes thirteen parameters, all of which `replace_memory` has in its `ReplacementRequest` and unpacks into scalars; `_replacement_fields` takes ten; `_replacement_write` returns an unannotated four-tuple that `_replacement_execute` unpacks and passes to `_replacement_response` as four more scalars (nine parameters). A typed plan replaces the dict; the request is passed whole.

Rejected: a mutable plan filled in stages (every late field would be Optional and mypy would need casts or asserts at about nine reads); leaving the dict and only trimming parameter lists (the dict is the state bag); a `TypedDict` (no way to express "filled later"; still string keys); changing `ReplacementRequest` or the public signatures; touching the transaction, savepoint or ordering logic.

## 2. Target shape

All private, in `lifecycle.py`, defined above `_replacement_content`. Everything the write needs that is known before the transaction lives in one frozen plan; what the transaction decides lives in two small frozen results. No field is Optional-until-filled, so mypy (`check_untyped_defs` is on) needs no cast or assert.

```python
@dataclass(frozen=True, slots=True)
class _ReplacementContent:
    """The validated new body of the replacement."""
    new_title: str
    new_content: str
    content_hash: str
    quality: dict
    now: str

@dataclass(frozen=True, slots=True)
class _ReplacementFields:
    """What the replacement inherits from the predecessor and what the caller supplied."""
    inherited_agent_id: str | None
    inherited_context: str | None
    inherited_scope: str | None
    inherited_type: str
    inherited_metadata: str | None
    effective_tags: list
    inherited: dict[str, Any]
    changed: dict[str, Any]

@dataclass(frozen=True, slots=True)
class _ReplacementPlan:
    """Everything one revise/supersede needs before its write transaction starts."""
    columns: list[str]
    before: dict
    orphaned_edges: list[dict[str, Any]]
    agent_session_id: str | None
    content: _ReplacementContent
    fields: _ReplacementFields
    reason: str
    retrieval_text: str | None
    retrieval_text_hash: str | None
    core_declaration: CoreDeclaration

@dataclass(frozen=True, slots=True)
class _ReplacementRow:
    """What the write transaction decided for the new entity."""
    new_id: str
    core_state: dict

@dataclass(frozen=True, slots=True)
class _ReplacementOutcome:
    """What the replacement write produced."""
    row: _ReplacementRow
    relation_id: str
    repointed_relations: list[dict[str, Any]]
    still_orphaned_ids: set[str]
```

(All five hold lists or dicts: add the same one-line "must not be hashed or cached" comment `ReplacementRequest` carries. `inherited_type` is a `str` because the existing expression always yields one; the nullable types keep the annotations of the dict values they replace. Nothing mutates a plan or a result after construction.)

Functions after the change (hand count, all positional):

- `_replacement_content(title, content, tags) -> tuple[_ReplacementContent | None, dict | None]`: 3 parameters; same body, returns the dataclass instead of a dict.
- `_replacement_fields(before, inherited_tags, request: ReplacementRequest, content: _ReplacementContent) -> _ReplacementFields`: 4 parameters; reads `request.tags`, `agent_id`, `context_id`, `scope`, `memory_type`, `metadata`, `reason`; the `inherited` and `changed` dicts are built by the same expressions (same key order, same `json.dumps(metadata)`). `request.reason` is `str | None`: use `cast(str, request.reason)` inside this function (the validator guarantees it; today the function receives the already cast value), so mypy accepts `reason.strip()`. Rename the comprehension loop variable `field` to `name` (clearer next to the new dataclasses; a nit from the review).
- `_replacement_prepare(conn, request, resolved_id) -> tuple[_ReplacementPlan | None, dict | None]`: 3 parameters; the same steps in the same order (snapshot, inherited tags only when `request.tags is None`, `UNKNOWN_ENTITY_ID` when there is no predecessor row, orphaned-edge worklist, content validation, fields) and then, moved here from `replace_memory`, the retrieval-text normalisation (`INVALID_RETRIEVAL_TEXT` is still returned after every content error) and `core_declaration = request.core or CoreDeclaration()`. `fields` raises nothing, so moving the retrieval-text step behind it changes no outcome. `plan.reason` is `request.reason` as passed (raw, not stripped). `title`, `content` and `reason` are `cast(str, ...)` of the validated request, cast inside this function; the `UNKNOWN_ENTITY_ID` message keeps naming the CALLER's id, `cast(str, request.entity_id)`, not `resolved_id`.
- `replace_memory`: after `_replacement_target`, `plan, error = _replacement_prepare(conn, request, resolved_id)`; on error return it; then `return _replacement_execute(conn, operation, resolved_id, plan, request)`. The lazy imports of `normalize_retrieval_text` and `_retrieval_text_hash` move with the code into `_replacement_prepare`.
- `_replacement_write(c, operation, resolved_id, plan, repoint_relations) -> _ReplacementOutcome`: 5 parameters. It computes `new_id = str(uuid.uuid4())` and `core_state = core_governance_service.resolve_derived_core_state(...)` (same arguments as today, read from the plan) in the same positions as today, builds `row = _ReplacementRow(new_id, core_state)`, calls the insert and repoint steps, and returns `_ReplacementOutcome(row, relation_id, repointed, still_orphaned_ids)`. The re-read `locked_before = _replacement_locked_source(c, resolved_id, plan.before)` stays first.
- `_replacement_insert_entity(c, plan, row, locked_before, resolved_id, operation) -> str` (6 parameters; returns `relation_id`, the new id now comes from `row`), `_replacement_enqueue_jobs(c, plan, row, replacement, resolved_id)` (5), `_replacement_insert_core_tags(c, plan, row)` (3): `context: dict` becomes `plan: _ReplacementPlan` plus `row`. The call `_replacement_repoint_edges(c, resolved_id, new_id, now)` keeps its four positional arguments (`row.new_id`, `plan.content.now`).
- `_replacement_response(operation, resolved_id, plan, outcome, repoint_relations) -> dict`: 5 parameters (was 9). It builds its own `changed = {**plan.fields.changed, **large_content_descriptor(plan.content.new_content)}` (a copy; the plan is never mutated) and then adds the `core` or `demote_core` entry to that copy exactly as today; the response dict is identical.
- `_replacement_execute(conn, operation, resolved_id, plan, request)`: 5, same body; the `partial(_replacement_write, ...)` call passes `plan=plan` (keyword) in place of `context=context`; `values` becomes `outcome`.
- The untyped `context` name no longer exists in the file.

## 3. Decisions

- D1. Behaviour is identical: the order of the first error returned for each invalid input (validator, then `_replacement_target`, then in `_replacement_prepare`: unknown predecessor row, content errors, `INVALID_RETRIEVAL_TEXT`; then inside the write: `INACTIVE_TARGET`/`TARGET_CHANGED` from `_replacement_locked_source`, then `CORE_DECISION_REQUIRED`/core governance errors), the response dict (same keys, same order, same values, including the `changed` dict of the response, which is now a copy and equals today's), the `changed`/`inherited` dict contents and key order for every combination of supplied and omitted fields, the savepoint and transaction boundaries, the `CORE_DECISION_REQUIRED`, `CoreGovernanceRejected` and `_LifecycleRejected` handling, the coordinator-connection re-raise and the connection closing.
- D2. `_replacement_snapshot` and `_replacement_repoint_edges` keep their signatures and names (patched or wrapped by tests); `_replacement_locked_source` keeps `(c, resolved_id, before)`. A replacement makes exactly two snapshot reads, in this order: in `_replacement_prepare`, then in `_replacement_locked_source` (`tests/test_lifecycle_replacements_characterization.py` 164 to 178 corrupts the second one to provoke `TARGET_CHANGED`); the refactor adds none.
- D3. `plan.before` stays the `dict` returned by `_replacement_snapshot`; the code that mutates or copies it (`_replacement_insert_entity` builds `replacement` from `locked_before`) is unchanged.
- D4. No `noqa`; mypy stays clean (`check_untyped_defs` is on); `dataclass` is already imported in `lifecycle.py` (`ReplacementRequest` uses it); no new import is needed.

## 4. Tests (characterization, green before and after)

The 12 or so replace/lifecycle suites listed in section 0 pin most behaviour. Add to `tests/test_replace_memory_v2.py`, with literal expected values from a run on the unchanged code and a fixed clock where the test needs one:

1. For `revise_memory` and for `supersede_memory`: the full `data` of a successful response compared as a dict with literals for every key except ids and timestamps (key SETS and order for those): `inherited`, `changed`, `inherited_fields`, `changed_fields`, `predicate`, `reason` (use a reason with surrounding whitespace, and pin exactly what the unchanged code returns for the stripped value in `changed` and for the top-level `reason` field).
2. Inheritance matrix: one test per field of `tags`, `agent_id`, `context_id`, `scope`, `memory_type`, `metadata` supplied vs omitted, asserting `inherited`/`changed` membership and values (the existing tests may pin some; name them and add the rest).
3. The order of errors when several are present. A truly unknown id is rejected earlier, in `_replacement_target`, so reach the prepare-stage `UNKNOWN_ENTITY_ID` with the existing seam `patch.object(lifecycle, "_replacement_snapshot", return_value=([], {}))` (`tests/test_lifecycle_replacements_characterization.py` 83): predecessor row missing before invalid content before `INVALID_RETRIEVAL_TEXT`. Also pin that `INACTIVE_TARGET`/`TARGET_CHANGED` come before a core-governance error inside the write.
4. `repoint_relations=True` with one outgoing and one incoming semantic edge: `repointed_relations` shape and `still_orphaned` content in the response (the existing patched-repoint test covers the failure path; add the success path if missing).

## 5. Out of scope

`ReplacementRequest`; the MCP tools; the other BL-064 slices; `archive_memory`, `fetch_memory_chunk` and other `lifecycle.py` functions (including their `noqa: PLR0911`).

## 6. Acceptance

1. `./verify` exits 0.
2. The section 4 tests pass on the unchanged code and after the change (the developer reports both runs; the architect reruns on the final tree).
3. `rg -n "context\[\"" src/saltmdb/domain/services/memory_service/lifecycle.py` returns nothing; `rg -n "_replacement_(prepare|fields|write|response)" tests scripts hooks` returns nothing.
4. `git status --short` lists only the two files of section 0.
5. Mutation check on a scratch copy, killer named for each: read `inherited_scope` from the request instead of the predecessor; drop `json.dumps` on metadata; swap `supersedes`/`revises`; set `plan.new_id` after the insert; reorder prepare so the content check precedes the unknown-entity check; return `still_orphaned_ids` empty; drop `plan.reason` strip in `changed`; move the retrieval-text check before prepare. A surviving mutant is reported.
6. Design read (architect and consultant): typed stages, at most 6 parameters per function (`_replacement_insert_entity` has 6), no string-keyed state, no splatted tuple, no Optional-until-filled field.

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| `_replacement_prepare` has 13 parameters, `_replacement_fields` 10, `_replacement_response` 9; one caller each | `lifecycle.py` 393, 451, 781; callers 479, 905, 987 | read; scout AST scan |
| `context` has 22 distinct string keys and about 60 reads, all inside `lifecycle.py` | `rg 'context\["'` over `lifecycle.py` | probed (counts above) |
| Only `_replacement_repoint_edges` (1 reference) and `_replacement_snapshot` (3) are referenced by tests | `rg` over tests | probed; the scout's 'none' was wrong |
| `replace_memory` is called from `lifecycle.py` 1021 and 1063 and `daemon/dispatch.py` 573 | scout | probed by the scout |
| `_retrieval_text_hash(None)` returns `None` | `memory_service/validation.py` 28 | read |
| Every `context[` read is in the eight functions named; no other `context` dict exists; no daemon or test code builds one; `inherited_context` is written and never read there | `rg` plus consultant key audit | probed (22 or 23 keys) |
| `plan.before` is not mutated in the flow; `changed` is mutated only in `_replacement_response` | consultant read | read |
| An unresolvable id returns from `_replacement_target` before prepare | `lifecycle.py` ~903 | consultant read |
| The MCP revise/supersede tools do not accept `metadata`, so only service-level callers reach the metadata branches | `mcp/tools.py` signatures; `daemon/dispatch.py` 540 to 591 | read |
