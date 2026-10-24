# SPEC-BL064-S9-STORE-CHAIN-TYPES

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 9: the internals of the store pipeline in `memory_service/write.py`. `store_memory`'s public signature is slice 10 (it builds the same types). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Characterization baseline (run before and after): `.venv/bin/python -m pytest tests/test_memory_write_characterization.py tests/test_core_governance_service.py tests/test_store_duplicate_probe_off_writer.py tests/test_store_duplicate_policy_phase5.py tests/test_store_contract_phase5.py tests/test_bug_fixes.py tests/test_retrieval_text.py tests/test_memory_type.py tests/test_entity_id_prefix_resolution.py tests/test_core_tag_sync.py tests/test_manage_relation_id_resolution.py tests/test_set_core_memory_v2.py tests/test_mcp_write_tools_v2.py tests/test_mcp_core_tools_v2.py -q` (the developer adds the other files that reach `store_memory`; there are 55 of them, `./verify` covers all).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `memory_service.store_memory(...)` (about 205 test calls) and `apply_duplicate_probe`. Only one private helper is patched or called by tests: `_legacy_update_guard` (`tests/test_memory_write_characterization.py` ~502, `patch.object(write, "_legacy_update_guard", return_value=(None, None))`: the name stays, the return stays a 2-tuple `(error | None, frozen_current | None)`, the arguments are free because the fake ignores them). `_store_raw_entity` is re-exported by `memory_service/__init__.py` (line ~61 and ~133) and keeps its name.
- Scope (may edit): `src/saltmdb/domain/services/memory_service/write.py`, ONLY: `_legacy_frozen_changes`, `_legacy_update_guard`, `_resolve_raw_core_state`, `_raw_existing_values`, `_enqueue_raw_embedding_jobs`, `_store_raw_entity`, `_evaluate_store_quality`, `_preview_store_core`, `_persist_store_entity`, `_probe_store_duplicates`, `_prepare_store_content`, `_apply_frozen_store_values`, `_prepare_store_guarded`, `_prepare_store_operation`, the body of `store_memory` between the argument validation and the response (its SIGNATURE is untouched), and the new types (defined after `_CORE_TAG` at line ~25 and before `_normalized_tag_key`, i.e. ABOVE the first annotated use at `_legacy_frozen_changes` (~97): the module has no `from __future__ import annotations`, so a type defined at `_RawEntityRow` (~446) would raise `NameError` at import; `_RawEntityRow` stays where it is); `tests/test_memory_write_characterization.py` (new characterization tests only, plus the one call-shape edit if `_legacy_update_guard` needs it, which it does not); `tests/test_memory_write_types.py` if the developer prefers a new file.
- Does not touch: `store_memory`'s signature and docstring (slice 10), `_validate_store_basics` (it takes the five score names; slice 10 groups them), `_normalize_store_inputs`, `_resolve_existing_entity_id` (6 parameters, unchanged), `_insert_raw_entity`/`_RawEntityRow` (already typed), `_write_raw_scd_history`, `_raw_metadata`, `_raw_retrieval_values`, tag helpers, `core_governance_service` (including `resolve_store_core_state`, 9 parameters, patched by tests), `apply_duplicate_probe` and the probe-sink dict shape (consumed by `daemon/dispatch.py`), `duplicates.py`, any SQL, any threshold.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`store_memory` builds a 17-key `request` dict and passes it through `_prepare_store_operation`, `_prepare_store_content` and `_prepare_store_guarded`, which read it by string key, produce a second untyped `content_state` dict (9 keys) and a third untyped `prepared` dict (7 keys) containing a `proposed` dict of about 24 keys that `_persist_store_entity` hands to `_store_raw_entity` and `_resolve_raw_core_state`, which read it again by key (with `.get` defaults for some, `[...]` for others, so a missing key is a `KeyError` at run time). Wide tuples carry state between helpers: `_evaluate_store_quality` returns five values, `_raw_existing_values` five, `_apply_frozen_store_values` six. Helpers take their inputs as long scalar lists: `_preview_store_core` 12 parameters, `_probe_store_duplicates` 10, `_enqueue_raw_embedding_jobs` 10, `_legacy_frozen_changes` 10, `_legacy_update_guard` 9. The core-governance values (`is_core`, `core_reason`, `core_exit_condition`, `core_review_after`, `detail_memory_ids`) travel together through all of them and already have a type, `CoreDeclaration` (`_shared.py`).

Rejected: leaving the dicts and trimming parameter lists (the dicts are the state bag); changing `store_memory`'s signature here (slice 10); touching `core_governance_service`; merging `_preview_store_core` with `_resolve_raw_core_state` (they repeat the same overdue-boundary and capacity logic, but one returns errors and the other raises: a separate decision, recorded as a backlog candidate).

## 2. Target shape

All private, in `write.py`, defined after `_CORE_TAG` and before `_normalized_tag_key` (see section 0):

```python
@dataclass(frozen=True, slots=True)
class _StoreRequest:
    """The validated arguments of one store_memory call that the pipeline reads."""
    content: str                                  # one cast where store_memory builds it, after _validate_store_basics
    title: str | None
    retrieval_text: str | None | object          # RETRIEVAL_TEXT_UNSET sentinel allowed
    tags: list | None
    agent_id: str                                 # one cast, after _validate_store_basics
    context_id: str | None
    agent_session_id: str | None
    scope: str
    entity_id: str | None
    weight: int
    core: CoreDeclaration
    annotations: EntityAnnotations                # metadata, memory_type (mirrors slice 10's public group)

class _StoreQuality(NamedTuple):
    quality_res: dict
    quality_status: str
    quality_flags_str: str

@dataclass(frozen=True, slots=True)
class _StoreContent:
    """The normalised content stage: redacted text, title, retrieval text, hash and quality."""
    redacted_content: str
    title: str
    retrieval_text: str | None
    retrieval_text_provided: bool
    context_id: str | None
    content_hash: str            # the hash of the NEW redacted text (used by _resolve_existing_entity_id)
    quality: _StoreQuality

@dataclass(frozen=True, slots=True)
class _StoreProposal:
    """What the pipeline will persist: the request after frozen-field resolution."""
    resolved_entity_id: str | None
    redacted_content: str
    title: str
    tags: list | None
    agent_id: str
    scope: str
    memory_type: str | None
    context_id: str | None
    core: CoreDeclaration
    weight: int
    metadata: dict | None
    content_hash: str            # the FROZEN stored hash when a frozen administrative update applies, else the content's
    quality: _StoreQuality
    quality_score: int           # read from quality.quality_res["quality_score"] in _prepare_store_guarded, where it is read today
    retrieval_text: str | None
    retrieval_text_provided: bool
    agent_session_id: str | None
    skip_scd_history: bool

@dataclass(frozen=True, slots=True)
class _PreparedStore:
    proposal: _StoreProposal
    effective_memory_type_preview: str | None

class _FrozenStoreValues(NamedTuple):   # the six values of _apply_frozen_store_values
    title: str
    redacted_content: str
    context_id: str | None
    content_hash: str
    tags: list | None
    metadata: dict | None

class _RawExistingState(NamedTuple):    # the five values of _raw_existing_values
    existing: tuple | None
    base_source_changed: bool
    retrieval_text: str | None
    retrieval_hash: str | None
    prior_metadata: str | None
```

(All `dataclass` types hold lists or dicts: the one-line "must not be hashed or cached" comment. `_StoreQuality` and the two NamedTuples are fine as tuples: at most 5 elements, named, each read by name. Imports: add `NamedTuple` to the `typing` import and `CoreDeclaration`, `EntityAnnotations` from `._shared`. The frozen content hash is an OVERRIDE: `_prepare_store_guarded` replaces the hash with `frozen_current["content_hash"]` (via `_apply_frozen_store_values`) and `_store_raw_entity` uses THAT value for `base_source_changed`, the stored row and the embedding job, while `_resolve_existing_entity_id` uses the NEW text's hash BEFORE the override: two hashes exist, and the proposal carries the one that is persisted.)

Functions after the change (hand count; the old parameter lists are in section 1):

- `_evaluate_store_quality(redacted_content) -> _StoreQuality | dict`: returns a rejection envelope (a `dict`) on a quality REJECT, exactly the envelope it returns today (same codes, `field`, warnings), else the `_StoreQuality`; the caller checks `isinstance(result, dict)`. 1 parameter. The content hash is NOT part of the result: `_prepare_store_content` calls `compute_content_hash(redacted_content)` right after a successful quality check (the same order as today, where the hash is computed after the REJECT test).
- `_prepare_store_content(request: _StoreRequest) -> _StoreContent | str | dict`: 1 parameter; the `metadata` argument of `_normalize_store_inputs` is `request.annotations.metadata`; the error is the `str` or `dict` that `_normalize_store_inputs` or the quality gate returns today.
- `_legacy_frozen_changes(conn, resolved_entity_id, current, request, content) -> list[str]` (5, was 10) and `_legacy_update_guard(conn, resolved_entity_id, request, content) -> tuple[dict | None, dict | None]` (4, was 9; keeps the name and the 2-tuple return). They read `content.title`, `content.redacted_content`, `content.context_id` and `request.tags`, `.agent_id`, `.scope`, `request.annotations.memory_type` (today they receive the title, content and context after normalisation).
- `_apply_frozen_store_values(metadata, frozen_current) -> _FrozenStoreValues`: 2 parameters, a named result instead of the 6-tuple.
- `_prepare_store_guarded(conn, request, content) -> _PreparedStore | str | dict`: 3 parameters; builds the `_StoreProposal` (the dict `proposed` of today, same values: the frozen-current values win for `title`, `redacted_content`, `context_id`, `content_hash`, `tags` (to `None`), `metadata` (merged when `None`), and through the ternaries for `agent_id`, `scope`, `memory_type`; they do NOT override `is_core`, `weight` or the core fields; `_apply_frozen_store_values` receives `request.annotations.metadata`, the proposal's `metadata` comes from its result, and the `memory_type` ternary falls back to `request.annotations.memory_type`) and calls `_preview_store_core(conn, proposal)`.
- `_preview_store_core(conn, proposal) -> tuple[str | dict | None, str | None]`: 2 parameters (was 12); keeps its 2-tuple `(error, effective_memory_type_preview)` return (the caller unpacks two names); the calls into `core_governance_service` (`parse_is_core`, `resolve_store_core_state` with its 9 keyword arguments, `resolve_effective_memory_type`, `enforce_overdue_boundary`, `check_capacity_admission`) keep their exact arguments and order.
- `_prepare_store_operation(conn, request) -> _PreparedStore | str | dict`: 2 parameters.
- `_persist_store_entity(conn, proposal) -> tuple[str | dict | None, str | None, bool]`: keeps its 3-tuple (an error, the entity id, `was_existing`).
- `_store_raw_entity(conn, proposal) -> tuple[str, bool]` and `_resolve_raw_core_state(conn, proposal, entity_id, was_existing_entity) -> dict` (4, was 5; `redacted_content` is `proposal.redacted_content`): read the proposal's attributes; every default of today's `.get(...)` is preserved by the proposal's own construction (`weight or 1` stays `proposal.weight or 1` at the point of use; `retrieval_text_provided` is a plain bool; `skip_scd_history` replaces the `"_skip_scd_history"` key).
- `_raw_existing_values(conn, entity_id, title, redacted_content, content_hash) -> _RawExistingState` (5 parameters, a named result instead of the 5-tuple).
- `_enqueue_raw_embedding_jobs(conn, row: _RawEntityRow, state: _RawExistingState, retrieval_text_provided) -> None`: 4 parameters (was 10; `_RawEntityRow` already carries `entity_id`, `title`, `redacted_content`, `content_hash`, `final_retrieval_text`, `final_retrieval_hash`); the lazy `embedding_service` import stays lazy (tests patch the two enqueue functions at their source module).
- `_probe_store_duplicates(conn, proposal, stored_entity_id, was_existing, probe_sink, db_path) -> list[dict]`: 6 parameters (was 10, all keyword-only); the sink entry dict `{title, content, agent_id, tags, context_id, exclude_ids, db_path}` is byte-identical (it is consumed by `apply_duplicate_probe` and `daemon/dispatch.py`).
- `store_memory` (body only): builds one `_StoreRequest` (with `CoreDeclaration(is_core, core_reason, core_exit_condition, core_review_after, detail_memory_ids)` and `EntityAnnotations(metadata, memory_type)`) from its arguments after `_validate_store_basics`, with ONE `cast(str, ...)` each for `content` and `agent_id` (the basics check guarantees both), calls `_prepare_store_operation`, narrows the result with `isinstance` (the error arm is a `str` or a `dict`) and reads `prepared.proposal.title`, `.redacted_content`, `.context_id`, `.tags`, `.quality.quality_res`, `.agent_id`, `.scope` and `prepared.effective_memory_type_preview` for the rest, and returns the same envelope (`id`, `message`, `submitted_tags`, `effective_tags`, `duplicate_candidates`, warnings, `effective` with `agent_id`, `context_id`, `scope`, `memory_type`, `agent_session_id`). The local rebinding of `title`, `context_id`, `agent_id`, `scope` from the prepared values is replaced by reads of the proposal.

## 3. Decisions

- D1. Behaviour is identical: every returned envelope (success and each rejection: `IMMUTABLE_MEMORY`, `REJECT_EXACT_DUPLICATE`, quality rejection, core capacity rejection, the string errors), the order of the checks (inputs, quality, entity-id resolution, hash collision, frozen-field guard, frozen values, core preview), the transaction (`write_transaction_retrying` around `_store_raw_entity`), the SCD history skipped exactly for a frozen administrative update, the persisted `content_hash` being the frozen one in that case (it drives `base_source_changed` and the embedding job), tags and `#core` sync, the embedding jobs, the duplicate probe after persistence, the librarian trigger, the `effective` block, the `finally` that closes an owned connection, and the `except Exception` that returns `INTERNAL_ERROR` with the same text.
- D2. Names that tests patch stay module attributes read at call time: `_legacy_update_guard`; `write.evaluate_memory_quality` (patched at `tests/test_memory_write_characterization.py` ~313, the INTERNAL_ERROR test) and `write.MAX_MEMORY_CONTENT_CHARS` (~104); `core_governance_service.resolve_store_core_state` (patched through a `side_effect` wrapper at ~291 and ~309; `parse_is_core` is not patched); the `embedding_service` enqueue functions at their source module; `check_duplicate_memories`.
- D3. `_StoreRequest.retrieval_text` carries the `RETRIEVAL_TEXT_UNSET` sentinel unchanged through `_normalize_store_inputs` (which already produces `retrieval_text_provided`).
- D4. No `noqa`, no change to any SQL or threshold, no new third-party import; `NamedTuple`, `dataclass`, `CoreDeclaration` and `EntityAnnotations` may need importing (check the file).

## 4. Tests

The pipeline is covered through about 205 `store_memory` calls. Add, with literals from a run on the unchanged code and only where no existing test asserts the behaviour (the developer lists the existing test per item first):
1. The frozen administrative update: a second `store_memory` for an existing title/owner/scope with changed core or retrieval fields and omitted `title`/`content`/`context_id`/`tags`: the stored row keeps the frozen values, no `_h_` history row is created (`skip_scd_history`), and `metadata` is merged.
2. `IMMUTABLE_MEMORY` for each frozen field changed on an existing entity (title, content, agent_id, scope, memory_type, context_id, tags): the first changed field is reported in `field`, the message lists them in order.
3. The scope-default exception: an existing private entity updated with the default `scope="shared"` and nothing else is NOT a disclosure attempt; the stored scope stays `private` and `effective.scope == "private"`.
4. The `effective` block of the success envelope for a fresh insert and for an update (`memory_type` is the previewed effective type: use `memory_type=None` on an update of a typed entity so that preview and request differ; `agent_id`/`scope`/`context_id` the persisted ones; `agent_session_id` is `store_memory`'s own argument).
5. `weight` of 0 or `None` is stored as 1 (`weight or 1`), `retrieval_text` omitted vs `None` vs a string (`retrieval_text_provided` semantics) for a fresh insert and an update.
6. The in-transaction core revalidation: the rejection of the SECOND `resolve_store_core_state` call is already pinned (`tests/test_core_governance_service.py` ~278 to 311: `test_transaction_governance_rejection_rolls_back` for a str and a dict payload, `test_transaction_value_error_is_returned_without_writes`; cite them). Add only the capacity half: `check_capacity_admission` (patched on `core_governance_service`) rejecting on its SECOND call returns the rejection payload and writes nothing.
7. The frozen content hash (the behaviour the first draft would have lost): an existing entity whose stored `content_hash` was altered directly in the test database to differ from the hash of its text, updated administratively (same title and content, a changed core or retrieval field): no new embedding job is enqueued for the source text and the row keeps its stored hash (`base_source_changed` is computed against the frozen hash).

## 5. Out of scope

Slice 10; `core_governance_service`; merging the preview and the in-transaction core resolution; `_validate_store_basics`; the sink dict; the MCP tools; any SQL.

## 6. Acceptance

1. `./verify` exits 0.
2. The new section 4 tests pass on the unchanged code and after the change (the developer reports both runs; the architect reruns on the final tree); no existing test is edited.
3. `rg -n 'proposed\["|content_state\["|prepared\["|\.get\("resolved_entity_id"' src/saltmdb/domain/services/memory_service/write.py` returns nothing, and `rg -n 'request\["' write.py` returns only the hits inside `apply_duplicate_probe` (the sink dict, out of scope). The string-keyed reads that remain by design are listed: the 5-key dict that `_normalize_store_inputs` returns (out of scope) and `_prepare_store_content` reads once; the `current` row dict of `_legacy_update_guard` (it is also returned as `frozen_current`); `core_state` from `core_governance_service`; `quality_res` from `evaluate_memory_quality`. `rg -n "def _(legacy_frozen_changes|legacy_update_guard|preview_store_core|enqueue_raw_embedding_jobs|probe_store_duplicates|resolve_raw_core_state)" -A14 write.py` shows the parameter counts of section 2 (judge: the architect reads the definitions).
4. `git status --short` lists only the files of section 0.
5. Mutation check on a scratch copy, killer named for each: the proposal's `scope`, `memory_type` or `context_id` taken from the request instead of the frozen current row (killable via item 3 and via an update with `memory_type=None`/`context_id=None` on an existing entity; the same mutant for `agent_id`, `title` and `content` is EQUIVALENT because the guard rejects any difference, do not list it); the proposal uses the NEW hash instead of the frozen one (killed by item 7); skip the SCD-history skip; `weight` not defaulted; `retrieval_text_provided` true when the sentinel is passed; drop `skip_scd_history`; report the wrong first changed field; treat the default `shared` scope as a change for an existing private entity; the probe sink entry loses `exclude_ids`; the success envelope's `effective.memory_type` taken from the request instead of the preview (use a case where they differ: `memory_type=None` on an update of a typed entity); the capacity check skipped in the transaction; the `#core` tag sync skipped; the embedding job skipped when `base_source_changed`; the quality REJECT envelope loses its warnings; hoist the `embedding_service` import to module level (killable here: a function-level `from ... import enqueue_*` binds the names at load time, so the tests' patches at the source module would no longer reach them). A surviving mutant is reported; accepted only with a proof of equivalence.
6. Design read (architect and consultant): at most 6 parameters per function, no string-keyed state, no tuple of more than 3 returned across helpers (the two exceptions in section 2 are named tuples of 5 and 6 read by name), `CoreDeclaration` reused.

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| `store_memory` builds a 17-key `request` dict; `_prepare_store_content` returns a 9-key dict; `_prepare_store_guarded` returns a 7-key dict containing the ~24-key `proposed` | `write.py` 1046 to 1199, 1240 to 1262 | read |
| `_store_raw_entity` and `_resolve_raw_core_state` read `proposed` by `[...]` and `.get(...)` | `write.py` 630 to 730, 275 to 357 | read |
| Parameter counts: `_preview_store_core` 12, `_probe_store_duplicates` 10, `_enqueue_raw_embedding_jobs` 10, `_legacy_frozen_changes` 10, `_legacy_update_guard` 9, `_resolve_raw_core_state` 5 | `write.py` | read; scout AST map |
| Only `_legacy_update_guard` is patched by tests and `_store_raw_entity` is re-exported; the other store helpers have no external references | `rg` over tests, scripts, hooks, src | probed by the architect |
| `weight or 1` is applied at the point of use in `_store_raw_entity`; the sentinel counts as 'not provided' in `_normalize_retrieval_input`; `_resolve_existing_entity_id` runs with the NEW hash before the frozen override | `write.py` ~639, ~808, ~1112 | consultant read |
| `CoreDeclaration` has `is_core`, `core_reason`, `core_exit_condition`, `core_review_after`, `detail_memory_ids` | `_shared.py` 34 to 40 | read |
| `_RawEntityRow` already carries entity id, title, content, hash and the final retrieval values | `write.py` 446 to 470 | read |
| `resolve_store_core_state` (9 parameters) has 7 external references and is patched by tests; it is out of scope | scout; `rg` | probed |
| `_preview_store_core` and `_resolve_raw_core_state` repeat the overdue-boundary and capacity logic | `write.py` 899 to 965, 275 to 357 | read |

## Amendment 1 (2026-10-24): order when S10 lands first

S10 may land before S9 (SPEC-BL064-S10 amendment 1). If it has, `store_memory` takes the grouped parameters (`provenance`, `core`, `annotations`, `scores`, `runtime`) and builds the 17-key `request` dict from them; S9 then replaces that dict build with the `_StoreRequest` build, reading the same values from the groups instead of the flat names, and locates `store_memory` and the dict by NAME, not by line number. Everything else in S9 (the typed stages, the signatures of the internals, tests and acceptance) is unchanged.
