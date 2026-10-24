# SPEC-BL064-S8-SEARCH-MEMORY-SIGNATURE

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 8: the service-layer `search_memory` signature, which holds one of the two open Sonar issues (`python:S107`, 30 parameters, limit 13). Shared `context_id`: `saltmdb-sonar-fix-2026-10`. It follows 7a and 7b (the pool internals) and uses the repository's own precedent of grouping parameters into small domain objects and pinning the result (`revise_memory` and `supersede_memory` take `provenance: Provenance` and are pinned at 13 parameters or fewer with a `TypeError` for the old flat keyword by `tests/test_provenance.py` ~132). `Provenance` itself is NOT reused here: in a search `agent_id` is the visibility owner (who is asking) and `context_id`/`agent_session_id` are match filters, not authorship (S1 of the design review).
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Characterization baseline (run before and after): the thirteen test files of 7a section 0, plus `tests/test_dispatch_types.py`, `tests/test_viewer_routes.py`, `tests/test_provenance.py`, `tests/test_search_equivalence_gate.py` if it exists (the developer names the file that covers `scripts/benchmarking/search_equivalence_gate.py`), `tests/test_run_evaluation_matrix.py`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `memory_service.search_memory(...)` through its callers; the new pin test of section 4.
- Scope (may edit): `src/saltmdb/domain/services/memory_service/orchestrator.py` (the `search_memory` signature, its docstring parameter section, and the reads of the grouped values in its body), `src/saltmdb/domain/services/memory_service/_shared.py` (the two new types, next to `Provenance`), `src/saltmdb/domain/services/memory_service/__init__.py` (exports, wherever `Provenance` is exported), `src/saltmdb/daemon/dispatch.py` (`_dispatch_search_memory`, ~320 to 360), `src/saltmdb/viewer/routes/search.py` (~47), the seven `scripts/benchmarking/` files that call `search_memory(` (`run_relevance_gate_holdout.py`, `eval_stats.py`, `benchmark_precision_snapshot.py`, `benchmark_search_option_matrix.py`, `benchmark_rerank_gap_gate.py`, `benchmark_cross_encoder_rerankers.py`, `run_evaluation_matrix.py`) and `scripts/benchmarking/search_equivalence_gate.py`, the test files that call or patch the service function (about 25: the developer lists them), and a new `tests/test_search_memory_signature.py`.
- Does not touch: the MCP tool `search_memory` in `mcp/tools.py` (12 parameters, unchanged; it reaches the service through the daemon), `search_primitives.py`, `ranking.py`, `validation.py`, `_SearchControls`, any SQL, any behaviour, any threshold.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`search_memory` takes thirty parameters: Sonar's last open issue of this kind. They are not independent. Identity: `agent_id` (the requester: the visibility rule is `(e.agent_id = ? OR e.scope = 'shared')`). Match filters: `tags_filter`, `tag_operator`, `metadata_filter`, `memory_type_filter`, `is_core`, `context_id`, `agent_session_id`. Thirteen tuning knobs for ranking and candidate generation that nearly every caller leaves at the default: `cross_encoder_candidate_cap`, `cross_encoder_text_cap_chars`, `use_chunk_candidates`, `oversampling_multiplier`, `candidate_window`, `chunk_weight`, `use_retrieval_text_candidates`, `retrieval_fts_weight`, `retrieval_vector_weight`, `disable_semantic`, `prefer_durable_types`, `demote_superseded`, `collapse_supersedes_families`. Grouping the two families (two small frozen types) takes the signature to 12 and keeps mypy checking every call site. About 70 of the roughly 207 service-level call sites pass at least one grouped name (the consultant counted 87 for the earlier shape; 24 call sites that pass only `agent_id` now stay untouched); the others do not change.

Decisions already taken (do not reopen): option D of the design consult. Rejected: a `**options` shim (hides the parameters without making calls any easier to read, switches off mypy's keyword checking at every call site, and reimplements Python's unknown-keyword `TypeError` by hand); a `TypedDict` with `Unpack` (the 28-field schema would exist twice with a parity test, and it contradicts the `Provenance` precedent); grouping only the tuning knobs (leaves 18 parameters, above the limit); a dual form that accepts old and new keywords for one slice (temporary branching; every caller is in this repository, so one atomic migration is possible).

## 2. Target shape

In `_shared.py`, next to `Provenance`, which also needs `Literal` added to its imports (frozen, slotted, all defaults equal to today's defaults; the one-line "must not be hashed or cached" comment on each type that carries a list or a dict):

```python
@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """What a search must match: tag, metadata, type, core, context and session filters."""
    context_id: str | None = None
    agent_session_id: str | None = None
    tags_filter: list | None = None
    tag_operator: Literal["AND", "OR"] = "AND"
    metadata_filter: dict | None = None
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"] | None = None
    is_core: bool | None = None

@dataclass(frozen=True, slots=True)
class SearchTuning:
    """Ranking and candidate-generation knobs; every default is the behaviour of a plain search."""
    cross_encoder_candidate_cap: int | None = None
    cross_encoder_text_cap_chars: int | None = None
    use_chunk_candidates: bool = False
    oversampling_multiplier: int | None = None
    candidate_window: int | None = None
    chunk_weight: float | None = None
    use_retrieval_text_candidates: bool = False
    retrieval_fts_weight: float | None = None
    retrieval_vector_weight: float | None = None
    disable_semantic: bool = False
    prefer_durable_types: bool = False
    demote_superseded: bool = False
    collapse_supersedes_families: bool = False
```

`search_memory` after the change (12 parameters: 10 kept flat plus the 2 groups; today every call is by keyword, so the whole list is keyword-only with a leading `*`, which also fails fast on a stray positional call; the scout and the consultant found no positional call):

```python
def search_memory(
    *,
    agent_id: str = None,
    query_keywords: str = None,
    explain_mode: bool = False,
    limit: int = 5,
    cursor: str = None,
    include_related: bool = True,
    return_diagnostics: bool = False,
    mode: Literal["strict", "broad", "history"] = "broad",
    criteria: SearchCriteria = SearchCriteria(),
    tuning: SearchTuning = SearchTuning(),
    db_connection=None,
    db_path: str = None,
) -> list | dict:
```

The group defaults are INSTANCES in the signature (frozen, so safe as shared defaults; ruff's selected rules include no mutable-default rule): no `x = x or X()` in the body, which would add about 3 to `search_memory`'s Sonar cognitive complexity (about 11 today, limit 15). Every read of an old name in the body becomes a read of the group attribute; the places are (consultant list): the validation calls at ~1392 to 1405 (chunk controls, collapse check, CE caps, retrieval controls), the diagnostics dict (`use_chunk_candidates`, `collapse_supersedes_families`, `use_retrieval_text_candidates`), `_build_search_filters` (`agent_id`, `context_id`, `agent_session_id`, `is_core`, `memory_type_filter`, `metadata_filter`), the tag helpers (`_search_tag_groups`, `_append_search_tag_filters`, `_explain_search`), the exact-title condition (`not collapse_supersedes_families`), `_semantic_search_db_path(db_path, disable_semantic)` and the 19 keywords of `_SearchControls(...)`. `orchestrator.py` imports `SearchCriteria` and `SearchTuning` from `._shared`. The values passed on to the validation functions, to `_SearchControls(...)` (19 keywords, unchanged), to the diagnostics dict and to the helpers are the SAME values as today; `_SearchControls`, the validators and everything below `search_memory` do not change.

Callers after the change: `daemon/dispatch.py` `_dispatch_search_memory` builds `SearchCriteria(...)` and `SearchTuning(...)` from its validated keyword arguments, and passes `agent_id` flat, (its `_optional_*` validators and its error messages are unchanged) and calls `memory_service.search_memory(...)` with the 12 parameters; `viewer/routes/search.py` adds a `criteria` group ONLY when a field is set (its `is_core` and `agent_session_id`), so an exact `assert_called_once_with(...)` without them stays valid; the benchmark scripts and the tests pass the groups. A test or script that passed none of the grouped names is not touched.

## 3. Decisions

- D1. Behaviour is identical for every input: every default, the validation order and messages, the diagnostics, the result shape, the visibility rules for `agent_id`, the exception types. A `Provenance()`, `SearchCriteria()` or `SearchTuning()` with defaults equals passing nothing.
- D2. No flat name survives: passing any of the 20 removed names raises `TypeError` (7 criteria: `context_id`, `agent_session_id`, `tags_filter`, `tag_operator`, `metadata_filter`, `memory_type_filter`, `is_core`; 13 tuning: the fields of `SearchTuning`). The 10 flat names that stay are `agent_id`, `query_keywords`, `explain_mode`, `limit`, `cursor`, `include_related`, `return_diagnostics`, `mode`, `db_connection`, `db_path`. There is no compatibility form.
- D3. `Provenance` is not used for search (see section 1); `agent_id` stays the flat requester, its visibility rule unchanged.
- D4. The codemod that migrates the roughly 25 test files and the scripts is a THROWAWAY script kept in the developer's scratch directory (`plans/`, gitignored) and NOT committed. It uses Python's `ast` (or `ast-grep`) to find calls whose callee is `search_memory` (a bare name or an attribute) and rewrites only the keyword arguments: it moves each removed name into the matching group constructor and adds the import. BEFORE writing it, scan `tests/` and `scripts/` with `ast` for test-local WRAPPER functions that forward `**kwargs` (or named arguments) to `search_memory` (for store_memory the consultant found ten, hiding 59 call sites from a callee-name codemod): list them in the report; the codemod treats their names as aliases of `search_memory` and their bodies are edited by hand. It edits TEXT SPANS (the node positions `lineno`, `col_offset`, `end_lineno`, `end_col_offset` or `libcst`-style cut and insert), NEVER `ast.unparse` (84 of the 87 calls are multi-line and 16 contain a `#` inside the call, so regenerating code would drop comments and reflow lines and could not be reviewed as mechanical). Calls with `**kwargs` and calls on a `tools.` receiver (the MCP tool, different signature) are NOT touched by the script; the alias `search` in the equivalence gate is a hand edit (D5); the `**kwargs` calls (3: `viewer/routes/search.py` 47, `tests/test_search_memory_characterization.py` ~156, `scripts/benchmarking/benchmark_precision_snapshot.py` ~190) and every `patch` whose assertions read flat `call_args.kwargs` are edited by hand. The developer reports the script and a count of the sites it rewrote, and the architect reviews the TEST diff as a mechanical one (section 7 item 3).
- D5. `scripts/benchmarking/search_equivalence_gate.py` runs one runner against whatever tree `--expect-src` points at, including an OLD tree with the flat `search_memory`, and `_call` (~668 to 697) records EVERY per-call exception as part of the compared output; its cases are built in code (`_base_arguments` ~147 to 179 passes the 28 flat names, plus the matrix and derived cases) and the artifacts and resume keys are JSON of those flat `arguments`, so the flat case format MUST stay. Add one local function that maps a flat case to the call ONLY when `"criteria"` is in `inspect.signature(search).parameters` (the new tree), builds the two groups from the flat case, and validates the flat names against the union of the 10 flat parameters and the two groups' fields; against an old tree it passes the flat arguments unchanged and validates against the old signature, so a reference run is never turned into a log of `TypeError`s. `tests/test_search_equivalence_gate.py` gains a test for both branches (a fake new-style search with groups and a fake old-style flat search); the existing tests at ~204 (unknown keyword -> `ValueError`), ~292 (`use_cross_encoder` rejected) and ~514, ~522 (a fake `**kwargs` search with validation bypassed) must keep passing unchanged.
- D6. No `noqa`; no behaviour change; the MCP tool and its tests are untouched.

## 4. Tests

New `tests/test_search_memory_signature.py`, modelled on `tests/test_provenance.py` ~132:
1. `len(inspect.signature(search_memory).parameters) <= 13` (it is 12).
2. For each of the 20 removed names of D2, `search_memory(**{name: <valid value>})` raises `TypeError`; a positional call raises `TypeError`.
3. Defaults: a table of the OLD defaults of all 30 parameters (literals, from the old signature) compared with `SearchCriteria()`, `SearchTuning()` and the 10 kept flat parameters' defaults (via `inspect.signature`); the group parameters' defaults are `SearchCriteria()` and `SearchTuning()` instances: equal.
4. Equivalence of forms: for three representative inputs (a plain search, a filtered search with `tags_filter`+`tag_operator`+`is_core`, a strict-mode search with tuning knobs set), the result with the groups equals the result of the old flat call. Obtain the old result from a literal recorded on the unchanged code (the developer records it BEFORE the change, as the characterization step).
5. `dispatch._dispatch_search_memory` maps its 28 keywords to `agent_id` + the two groups: a test patching `memory_service.search_memory` and asserting the `criteria` and `tuning` values it receives for a request that sets one field of each group. The existing `tests/test_dispatch_types.py` 23 to 32 asserts flat `call["tag_operator"]`, `call["prefer_durable_types"]` and `call["demote_superseded"]`: they become `call["criteria"].tag_operator` and `call["tuning"].prefer_durable_types` / `.demote_superseded` (`explain_mode`, `limit`, `mode`, `include_related` stay flat); the store test at ~15 is unaffected.
6. The viewer route: `tests/test_viewer_routes.py` 149 to 165 (an exact `assert_called_once_with` of `query_keywords`, `limit=50`, `include_related=False`, `mode="broad"`, `db_path`) stays unchanged because the route adds `criteria` only when a field is set; 168 to 182 becomes `criteria=SearchCriteria(agent_session_id="sess-alpha")` (dataclass equality makes the assertion work).

## 5. Call-site migration (mechanical)

Every existing test and script that passes a removed name is rewritten by the D4 codemod or by hand; no assertion other than a flat `call_args.kwargs` read changes. The files with such reads (hand edits): `tests/test_search_accuracy_stage2.py` TWO tests: 561 to 587 (`benchmark_matrix.run_one_config`: the seven flat reads become `tuning.*` reads and the script's call changes too) and 589 to 610 (the dispatch-forwarding test: the six tuning reads become `tuning.*`; `return_diagnostics` stays flat), `tests/test_dispatch_types.py` 23 to 32, `tests/test_viewer_routes.py` 168 to 182, and any other the developer finds with `rg "call_args.*kwargs"` near `search_memory`.

## 6. Out of scope

`store_memory` (slices 9 and 10); the MCP tool; the pool internals (7a and 7b); `_SearchControls`; a Sonar re-scan (the architect checks the gate after the push).

## 7. Acceptance

1. `./verify` exits 0.
2. The characterization baseline passes before the change (with the section 4 items 3 and 4 written first) and after it.
3. The architect reviews the test and script diff as mechanical: `git diff -U0 -- tests scripts | rg "^[-+]"` shows, for rewritten calls, only keyword lines moved into a group constructor, added imports, and the hand edits of section 5; no assertion changes outside section 5 (judge: the architect, with an AST comparison of old and new call arguments produced by the developer's codemod report).
4. An AST check (the codemod emits it, the architect reruns it with `.venv/bin/python -I`): no call of the service `search_memory` (a bare name or an attribute, excluding a `tools.` receiver) in `src`, `scripts` and `tests` has a removed keyword among its arguments, and none has a positional argument (`rg` cannot do this: 84 of the calls span several lines, so a keyword grep on the call's own line would pass an incomplete migration). The new D2 `TypeError` test and `./verify` catch any leftover at runtime.
5. `git status --short` lists only the files of section 0 and nothing untracked other than `tests/test_search_memory_signature.py`.
6. Mutation check on a scratch copy, killer named for each: a different default in one of the two groups (one mutant per group); read `criteria.context_id` as `agent_session_id`; read `agent_id` from the criteria; ignore `criteria.tag_operator`; ignore `tuning.disable_semantic`; read a tuning knob from the wrong field; drop the `*` (positional call allowed); keep an old flat keyword accepted; `dispatch` leaves `is_core` out of the criteria; the viewer drops `agent_session_id`; the gate maps flat cases to groups against an OLD tree. A surviving mutant is reported.
7. Design read (architect and consultant): 12 parameters, two coherent groups with domain names, no flat name left, the codemod not committed.
8. After the push the architect reads the Sonar gate: the `search_memory` S107 is no longer open.

## 8. Claims

| Claim | Where | Verified |
|---|---|---|
| `search_memory` has 30 parameters, all with defaults, callable by keyword; no positional call exists | `orchestrator.py` ~1287 to 1317; scout and consultant AST counts | read; probed |
| 207 service-level call sites; 87 pass at least one name that was grouped in the first draft (34 identity, 37 filter, 28 tuning); of the 34 identity ones 24 pass ONLY `agent_id`, which now stays flat, so about 70 change; 84 of the rewrites span several lines and 16 contain a `#`; 0 positional calls; 3 use `**` | consultant AST count over tests, scripts, src, hooks | measured by the consultant |
| In `search_memory` the only reads of `agent_id`, `context_id` and `agent_session_id` are in `_build_search_filters` (visibility owner; match filters) | `orchestrator.py` ~598 to 628 | consultant read |
| `search_memory`'s Sonar cognitive complexity is about 11 today; McCabe 8; three `x or X()` defaults would add 3 | consultant hand count and ruff C901 | estimated; the architect measures McCabe with ruff before and after |
| The precedent: `revise_memory`/`supersede_memory` take `provenance`, pinned by a parameter-count and `TypeError` test | `lifecycle.py` ~1000 to 1014; `tests/test_provenance.py` ~132 to 140 | read |
| `dispatch._dispatch_search_memory` passes 28 keywords; the viewer route passes `is_core` and `agent_session_id` via `**search_kwargs` | `dispatch.py` ~320 to 360; `viewer/routes/search.py` ~47 | read by the consultant and the scout |
| `search_equivalence_gate.py` validates case args via `inspect.signature(search_memory)` | `scripts/benchmarking/search_equivalence_gate.py` ~110 to 114 | read by the consultant |
| No test asserts `TypeError` on a bad keyword to `search_memory` today | consultant rg | probed |
| `tests/test_search_accuracy_stage2.py` ~574 to 610 asserts flat `call_args.kwargs` on a patched `benchmark_matrix.search_memory` | consultant | read |
| `tests/test_dispatch_types.py` ~15, ~23 and `tests/test_viewer_routes.py` ~149, ~168 patch the service functions; their assertions are unread | consultant | NOT read: the developer reads them first |
| The S107 limit is 13 and `self`/`cls` are exempt; the rule does not say how `*args`/`**kwargs` count | Sonar rule text | read (the shim was rejected, so it does not matter) |

## Amendment 1 (2026-10-24): S8 may land before 7b; the test wrapper translates the flat keys

The consultant's ordering read (HEAD 144237f) found no code, type or field dependency of S8 on 7a or 7b: S8 edits the arguments of `_build_search_filters`, the tag helpers, `_semantic_search_db_path`, the 19 keywords of the `_SearchControls(...)` constructor and the validation calls; 7a edits only the `_search_window_pool(...)` call and one new line before it; 7b does not touch `search_memory` and adds one read-only property to `_SearchControls`, which S8 does not touch. The only shared file is `tests/test_search_memory_characterization.py`, a sequencing constraint (one writer at a time), not a dependency.

A1.1 The sentence in section 0 "It follows 7a and 7b" is superseded: S8 has no dependency on 7a or 7b and may land immediately after 7a and before 7b, or after both. S8 must not be implemented while a 7a or 7b diff is uncommitted on the tree.

A1.2 Section 3 D4, the test wrapper: in `tests/test_search_memory_characterization.py`, `_search_case(conn, path, options)` translates the removed flat keys into the groups INSIDE the wrapper, with ONE table mapping the seven `SearchCriteria` names and the thirteen `SearchTuning` names (the twenty removed parameters) to their group; it builds `criteria=SearchCriteria(...)` and `tuning=SearchTuning(...)` from the keys it finds in `options`, and passes only non-empty groups. The many `_search_case({...})` callers keep their flat option dicts: the codemod does not rewrite the dicts of `_search_case` callers, only calls whose callee is `search_memory` (or another forwarding wrapper listed under D4). New tests written for 7b through `_search_case` use the existing flat style.

A1.3 The `T ~nnn` test-line citations of section 4 and 5 drift after the codemod adds lines; locate every test by its NAME. The claim in section 8 about `dispatch.py` forwarding the flat names is true before S8 only; after S8 dispatch builds `SearchTuning(...)`.

A1.4 Nothing else changes: scope, target types, decisions D1 to D3 and D5, tests and acceptance are as locked.

## Amendment 2 (2026-10-24): two wording corrections to amendment 1

A2.1 `_search_case` is excluded from the D4 alias list: it is a forwarding wrapper that A1.2 handles by translation, so the codemod does not treat it as an alias of `search_memory`. In A1.2, "another forwarding wrapper listed under D4" reads "reported under D4" (the list the developer reports).

A2.2 The dispatch claim of amendment 1.3 applies to section 8 of this spec ("`dispatch._dispatch_search_memory` passes 28 keywords"): true before S8 only.
