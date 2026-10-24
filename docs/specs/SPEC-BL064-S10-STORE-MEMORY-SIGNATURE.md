# SPEC-BL064-S10-STORE-MEMORY-SIGNATURE

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 10: the service-layer `store_memory` signature, which holds the other of the two open Sonar issues (`python:S107`, 25 parameters, limit 13). Shared `context_id`: `saltmdb-sonar-fix-2026-10`. It follows slice 9 (the typed pipeline: `_StoreRequest` already mirrors the groups below) and uses the repository's precedent (`revise_memory`/`supersede_memory` take `provenance: Provenance`, pinned at 13 parameters or fewer with a `TypeError` for the old flat keyword by `tests/test_provenance.py` ~132) and the design of slice 8.
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Characterization baseline (run before and after): the files of slice 9 section 0, plus `tests/test_dispatch_types.py`, `tests/test_viewer_routes.py`, `tests/test_provenance.py`, `tests/test_mcp_tools.py` (the MCP surface reaches `store_memory` through the daemon).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `memory_service.store_memory(...)` through its callers; the new pin test of section 4.
- Scope (may edit): `src/saltmdb/domain/services/memory_service/write.py` (the `store_memory` signature, its docstring parameter section, `_validate_store_basics`, and the packing into `_StoreRequest`), `.../_shared.py` (two new types next to `Provenance`), `.../__init__.py` (exports, wherever `Provenance` is exported), `src/saltmdb/daemon/dispatch.py` (`_dispatch_store_memory`, ~284 to 320), `scripts/benchmarking/build_diverse_test_db.py` (the one script call, ~554), the test files that call or patch the service function (about 55: the developer lists them), and a new `tests/test_store_memory_signature.py`.
- Does not touch: the MCP tool `store_memory` in `mcp/tools.py` (10 parameters, unchanged; it reaches the service through the daemon with FLAT keyword arguments, and the wire format of the daemon RPC does not change), `core_governance_service`, `consolidate_memories`, `revise_memory`, `supersede_memory`, the slice 9 internals beyond the construction of `_StoreRequest`, any SQL, any behaviour, any threshold.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`store_memory` takes twenty-five parameters. Eight are the entity's own fields (`content`, `tags`, `scope`, `title`, `entity_id`, `retrieval_text`) and the two storage handles (`db_connection`, `db_path`) that every service function takes. The other seventeen form four families that already have names or obvious ones: provenance (`agent_id`, `context_id`, `agent_session_id`: the existing `Provenance`), the core declaration (`is_core`, `core_reason`, `core_exit_condition`, `core_review_after`, `detail_memory_ids`: the existing `CoreDeclaration`), annotations (`metadata`, `memory_type`: the existing `EntityAnnotations`), the quality scores (`weight`, `relevance`, `impact`, `novelty`, `actionability`: a new small type), and two pieces of runtime plumbing that only the daemon uses (`coordinator`, `probe_sink`: a new small type). Grouping them takes the signature to 13, the limit, and keeps mypy checking every call site. `Provenance` appears at 202 of the roughly 207 call sites, so this is a wide but mechanical migration (about 55 test files).

Decisions already taken (do not reopen): option D of the design consult, see slice 8. Rejected: a `**options` shim and a `TypedDict` with `Unpack` (see slice 8); a dual form for one slice; leaving the issue open with a reviewed Sonar 'accept' (the fallback if the churn is rejected, not chosen). The parameter count after the change is exactly 13 (the limit, so a future added parameter fails the pin test and the Sonar rule: that is intended, not a margin to spend).

## 2. Target shape

In `_shared.py`, next to `Provenance`, `CoreDeclaration` and `EntityAnnotations` (frozen, slotted; the one-line "must not be hashed or cached" comment where a list or dict is carried):

```python
@dataclass(frozen=True, slots=True)
class StoreScores:
    """The quality dimensions of a stored memory; weight defaults to 1, the rest to None."""
    weight: int = 1
    relevance: int | None = None
    impact: int | None = None
    novelty: int | None = None
    actionability: int | None = None

@dataclass(frozen=True, slots=True)
class StoreRuntime:
    """How a store runs inside the daemon: the write coordinator and the deferred duplicate-probe sink."""
    coordinator: Any = None
    probe_sink: list[dict[str, Any]] | None = None
```

`store_memory` after the change (13 parameters; `content` stays positional-or-keyword so the six existing positional calls keep working, everything else is keyword-only):

```python
def store_memory(
    content: str = None,
    *,
    tags: list = None,
    scope: Literal["private", "shared"] = "shared",
    title: str = None,
    entity_id: str = None,
    retrieval_text: str | None | object = RETRIEVAL_TEXT_UNSET,
    provenance: Provenance = Provenance(),
    core: CoreDeclaration = CoreDeclaration(),
    annotations: EntityAnnotations = EntityAnnotations(),
    scores: StoreScores = StoreScores(),
    runtime: StoreRuntime = StoreRuntime(),
    db_connection=None,
    db_path: str = None,
) -> str | dict:
```

(Group defaults are frozen INSTANCES in the signature: no `x = x or X()` in the body, which would add to `store_memory`'s complexity. `tags` and `scope` were positional-or-keyword before; the scout found no caller that passes them positionally.) `_validate_store_basics` is reduced to `(agent_id, content, scope, memory_type, scores) -> (error | None, weight)` (5 parameters, was 9). `store_memory` builds the slice 9 `_StoreRequest` from the groups (`provenance.agent_id` with one `cast`, `annotations`, `core`) and reads `runtime.coordinator` and `runtime.probe_sink` where it read the flat names. The docstring's parameter text is updated for the groups; its core-governance paragraph is kept.

Callers after the change: `daemon/dispatch.py` `_dispatch_store_memory` builds `Provenance`, `CoreDeclaration`, `EntityAnnotations`, `StoreScores` and `StoreRuntime(coordinator=..., probe_sink=...)` from its validated flat keyword arguments (its scope and weight validation and the `RETRIEVAL_TEXT_UNSET` sentinel logic are unchanged) and calls `memory_service.store_memory(...)`; the benchmark script and the tests pass the groups. A call that passed none of the grouped names (only `content`, `tags`, `scope`, `title`, `entity_id`, `retrieval_text`, `db_connection`, `db_path`) is not touched.

## 3. Decisions

- D1. Behaviour is identical for every input: every default, the validation order and messages (`_validate_store_basics` still derives `weight` from the dimensions exactly as today), the response envelope, the exception types. `StoreScores()`, `Provenance()`, `CoreDeclaration()`, `EntityAnnotations()` and `StoreRuntime()` with defaults equal passing nothing.
- D2. No flat name survives: passing any of the 17 removed names raises `TypeError`: `agent_id`, `context_id`, `agent_session_id` (3), `is_core`, `core_reason`, `core_exit_condition`, `core_review_after`, `detail_memory_ids` (5), `metadata`, `memory_type` (2), `weight`, `relevance`, `impact`, `novelty`, `actionability` (5), `coordinator`, `probe_sink` (2). There is no compatibility form. A positional argument beyond `content` raises `TypeError`.
- D3. `Provenance` keeps its meaning (authorship of a write); `EntityAnnotations.memory_type` is the REQUESTED type (the effective type is still resolved by the pipeline).
- D4. The codemod is the throwaway of slice 8 D4 (text spans, never `ast.unparse`; kept in scratch, not committed; `**kwargs` calls, `tools.` receivers and flat `call_args.kwargs` assertions edited by hand). THE REAL HAND COST IS BEHIND TEN TEST-LOCAL `**kwargs` WRAPPERS (consultant AST scan): test functions that forward `**kwargs` to `store_memory`, with 59 call sites that pass removed names THROUGH the wrapper, invisible to a callee-name codemod: `_store_core` (82 calls, 25 with removed names) and `_store_normal` (17 / 3) in `tests/test_core_governance_service.py` (~397, ~410; both `kwargs.pop` `agent_id`, `core_reason`, `core_exit_condition` and hard-code `is_core=True`); `store` in `tests/test_memory_write_characterization.py` (~25; 42 / 13; its default dict holds `agent_id` and `probe_sink`; called with `weight=4` at ~502); `_store` in `tests/test_store_duplicate_probe_off_writer.py` (~59; 15 / 5), `tests/test_memory_type.py` (~140; 11 / 4), `tests/test_set_core_memory_v2.py` (~59; 23 / 2), `tests/test_core_tag_sync.py` (~29; 7 / 7); the wrappers in `tests/test_retrieval_text.py` (~61), `tests/test_entity_id_prefix_resolution.py` (~24) and `tests/test_manage_relation_id_resolution.py` (~15) have no removed names at their call sites (only the wrapper body changes); `create_entity` in `tests/test_viewer_routes.py` (~1180; its body passes `is_core`, `memory_type`, `agent_id` itself plus `**core_kwargs`). Decision: the codemod treats these wrapper names as aliases of `store_memory` (rewrite the calls through them into group constructors) and the ten wrapper bodies are edited by hand to forward the groups; no shared flat-vocabulary test helper (it would re-create the old flat API in a second place beside `dispatch`). The developer re-derives the wrapper list with an AST scan of `tests/` for functions that call `store_memory` with `**` before starting (and for any wrapper of the same kind added since), and reports it.
- D5. The migration is ONE atomic change (src, scripts, dispatch, the pin test and every test file together), because the removal of the flat names must leave no red intermediate state. It touches about 55 test files: the developer reports the count of rewritten sites and the list of hand-edited sites, and the architect reviews the test diff in parts (by directory) as a mechanical change. No dual form, no split commits.
- D6. No `noqa`; no behaviour change; the MCP tool and the daemon wire format are untouched.

## 4. Tests

New `tests/test_store_memory_signature.py`, modelled on `tests/test_provenance.py` ~132:
1. `len(inspect.signature(store_memory).parameters) <= 13` (it is 13).
2. For each of the 17 removed names, `store_memory(**{name: <valid value>})` raises `TypeError`; `store_memory("text", [])` (a second positional argument) raises `TypeError`; `store_memory("text")` with a database still works positionally.
3. Defaults: a table of the OLD defaults of all 25 parameters (literals from the old signature) compared with `StoreScores()`, `StoreRuntime()`, `Provenance()`, `CoreDeclaration()`, `EntityAnnotations()` and the 8 kept parameters' defaults (via `inspect.signature`).
4. Equivalence of forms: for three representative inputs (a plain store; a core store with `core_reason`/`core_exit_condition`/`detail_memory_ids`; a store with `weight`, the four dimensions, `metadata`, `memory_type`, `context_id`, `agent_session_id`), the stored rows and the returned envelope equal the literals recorded on the unchanged code BEFORE the change.
5. `dispatch._dispatch_store_memory` maps its 23 keywords to the five groups: a test patching `memory_service.store_memory` and asserting the group values it receives for a request that sets one field of each group, including `probe_sink` and the coordinator (`tests/test_dispatch_types.py` ~15 patches the service function: read what it asserts and adapt it minimally).
6. The viewer-route tests that call `store_memory` through `create_entity` (`tests/test_viewer_routes.py` ~1180) are adapted (a wrapper body, see D4).
7. `trigger_librarian` receives the coordinator: patch `librarian_service.trigger_librarian` (module attribute) and assert the `coordinator` it is called with for a store that passes `StoreRuntime(coordinator=<sentinel>)` (no test asserts it today).

## 5. Call-site migration (mechanical)

As slice 8 section 5: the D4 codemod (extended to the ten wrapper names of D4) rewrites the calls; the hand edits are the ten wrapper bodies, the flat reads on a patched `store_memory` (`tests/test_dispatch_types.py` ~15 to 21 asserts `call["scope"] == "shared"` (stays flat), `call["weight"] == 1` (becomes `call["scores"].weight`) and `assertNotIn("skip_duplicate_check", call)` (unchanged); no other patch of the service `store_memory` exists in tests), and nothing in `scripts/benchmarking/build_diverse_test_db.py` ~554 beyond what the span-based codemod does (the call has a comment block inside it).

## 6. Out of scope

`search_memory` (slice 8); the MCP tools; the slice 9 internals; `core_governance_service`; a Sonar re-scan (the architect checks the gate after the push).

## 7. Acceptance

1. `./verify` exits 0.
2. The characterization baseline passes before the change (with section 4 items 3 and 4 written first) and after it.
3. The architect reviews the test and script diff as mechanical: `git diff -U0 -- tests scripts` shows only keyword lines moved into a group constructor, added imports and the hand edits of section 5 (judge: the architect, with the developer's codemod report: the count of rewritten sites, the list of hand-edited sites).
4. An AST check (the codemod emits it, the architect reruns it with `.venv/bin/python -I`): no call of the service `store_memory` (a bare name or an attribute, excluding a `tools.` receiver) AND no call of any of the ten wrapper names of D4 in `src`, `scripts` and `tests` has a removed keyword among its arguments or more than one positional argument (the wrappers' bodies are checked by the runtime: `./verify`).
5. `git status --short` lists only the files of section 0 and nothing untracked other than `tests/test_store_memory_signature.py`.
6. Mutation check on a scratch copy, killer named for each: a different default in one of the five groups (one mutant per group); read `scores.weight` as `relevance`; ignore `core.detail_memory_ids`; ignore `annotations.metadata`; drop `provenance.agent_session_id`; ignore `runtime.probe_sink` (the deferred duplicate probe must still be deferred in the daemon: killed by `tests/test_store_duplicate_probe_off_writer.py`); ignore `runtime.coordinator` (killed by a NEW test of section 4 item 7: patch `librarian_service.trigger_librarian` and assert its `coordinator` argument for a store with `StoreRuntime(coordinator=<sentinel>)`); keep an old flat keyword accepted; allow a second positional argument; `dispatch` leaves `probe_sink` out of the runtime; `dispatch` maps `memory_type` into the wrong group field. A surviving mutant is reported.
7. Design read (architect and consultant): 13 parameters, five coherent groups with domain names, no flat name left, the codemod not committed.
8. After the push the architect reads the Sonar gate: the `store_memory` S107 is no longer open and no new issue appears.

## 8. Claims

| Claim | Where | Verified |
|---|---|---|
| `store_memory` has 25 parameters; only `content` is ever passed positionally (6 calls in `tests/test_bug_fixes.py`) | `write.py` ~1202 to 1227; consultant AST count | read; probed |
| 207 service-level call sites (205 in 55 test files, 1 in `dispatch.py` ~289, 1 in `scripts/benchmarking/build_diverse_test_db.py` ~554); provenance names at 202, core names at 47, scores at 4; 12 calls use `**` | scout and consultant counts | measured |
| `dispatch._dispatch_store_memory` passes 23 keywords and the sentinel logic for `retrieval_text` | `dispatch.py` ~284 to 320 | read by the scout |
| `Provenance`, `CoreDeclaration`, `EntityAnnotations` exist, frozen and slotted | `_shared.py` 27 to 46 | read |
| The MCP tool `store_memory` calls the daemon with FLAT keyword arguments and builds `retrieval_text_provided` | `mcp/tools.py` ~516 | read by the scout; unchanged |
| The precedent pin test | `tests/test_provenance.py` ~132 | read |
| `_validate_store_basics` has 9 parameters and returns `(error, weight)` | `write.py` ~787 | read |

## Amendment 1 (2026-10-24): S10 may land before S9

The Sonar issue is the 25-parameter signature of `store_memory`, and the signature change does not need S9's typed pipeline. At HEAD `store_memory` builds the 17-key `request` dict itself and passes it to `_prepare_store_operation`; `_StoreRequest` does not exist until S9.

A1.1 Wherever this spec says `store_memory` builds or packs the slice 9 `_StoreRequest` (section 0 scope, section 2), read: until S9 has landed, `store_memory` builds the existing `request` dict from the same values, with the same 17 keys in the same order and the same values as today (`provenance.agent_id` with the one `cast` the spec asks for, `provenance.context_id`, `provenance.agent_session_id`, the annotations, the core declaration fields, the scores, `runtime.coordinator`, `runtime.probe_sink`), so nothing downstream of that dict changes; after S9 has landed, it builds `_StoreRequest` as the spec says. The groups are unpacked at that single place.

A1.2 "It follows slice 9" in section 0 is superseded: S10 may land before or after S9. S10 must not be implemented while a S9 diff is uncommitted on the tree.

A1.3 Nothing else changes: the group types, the 13-parameter pin, D1 to D3, the caller migration, tests and acceptance are as locked. Acceptance greps that name `_StoreRequest` apply only once S9 has landed.
