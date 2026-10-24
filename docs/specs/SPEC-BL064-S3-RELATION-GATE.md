# SPEC-BL064-S3-RELATION-GATE

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 3 (gate measurement tuples). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `ae977ae` or later; `./verify` exits 0 at `ae977ae` (3632 passed, 10 skipped, 391 subtests passed; one run in two showed the unrelated teardown flake of BL-083, rerun once). Targeted baseline: `.venv/bin/python -m pytest tests/test_relation_service.py tests/test_mcp_tools.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `store_relation` (public, 118 test calls) and `manage_relation` through it; `bulk_store_relations`. No test calls the private helpers.
- Scope (may edit): `src/saltmdb/domain/services/relation_service.py` (the functions `_relation_gate_rejection`, `_apply_relation_gate`, `_relation_gate_measurements`, `_insert_relation_write`, `_store_relation_write`, lines 194 to 530 except `_check_core_relation_guard`, and the two call sites in `store_relation`, lines 708 to 735), `tests/test_relation_service.py` (a new characterization test only if section 4 finds a behaviour no existing test asserts; expected: none).
- Does not touch: `store_relation`'s signature and docstring, `invalidate_relation`, `_check_core_relation_guard`, `_normalize_predicate_name`, any SQL text, any threshold constant, any other module.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The relation write path threads the same values through five private functions as scalars. `_relation_gate_measurements` returns an unannotated four-tuple `(sim, offending, unresolved, contradictions)` that `_store_relation_write` unpacks and passes on as four separate arguments to `_apply_relation_gate` (11 parameters), which passes four of them on again to `_relation_gate_rejection` (6). `_store_relation_write` itself takes ten parameters, all of which `store_relation` has just validated and forwards unchanged from two call sites, and `_insert_relation_write` takes nine. Two small named types remove the splatting: the measurement result, and the validated write request. Sonar does not flag any of these today (limit 13); this is the quality half of BL-064.

Rejected: a dict for the measurement (a bag); changing `store_relation`'s 11-parameter public signature (118 test calls; not flagged); touching the gate's thresholds or messages.

## 2. Target shape

All private, in `relation_service.py`, defined just above `_relation_gate_rejection`:

```python
@dataclass(frozen=True, slots=True)
class _GateMeasurement:
    """What the relation gate measured for one candidate edge."""
    similarity: float
    offending: tuple[str, str] | None
    unresolved: dict[str, str]
    contradictions: list[str]

@dataclass(frozen=True, slots=True)
class _RelationWriteRequest:
    """One validated store_relation call, ready for the transactional write."""
    source_id: str          # resolved
    target_id: str          # resolved
    predicate: str          # as requested; canonicalised inside the write
    valid_at: str | None
    override_justification: str | None
    agent_id: str | None
    relation_id: str
    now: str
    allow_core_elaborates_on: bool
```

(`offending` is a pair of strings, `(id, id)` from `min_pairwise_cohesion` or `(id, "<reason>")` for an unresolved entity, or `None`; `min_pairwise_cohesion` is annotated `-> tuple[float, tuple[str, str] | None]`. `unresolved` and `contradictions` are never mutated; do not add a mutation. Both dataclasses are private and unhashable-by-content: add the one-line comment `# not hashable: carries a dict/list` on `_GateMeasurement`.)

Functions after the change (hand count, excluding nothing, all positional):

- `_relation_gate_measurements(conn, request: _RelationWriteRequest, canonical_predicate: str) -> _GateMeasurement`: 3 parameters; the body is today's, reading `request.source_id`/`request.target_id`, returning `_GateMeasurement(sim, offending, unresolved, contradictions)`.
- `_relation_gate_rejection(canonical_predicate, measurement: _GateMeasurement, violations)`: 3.
- `_apply_relation_gate(conn, request, canonical_predicate, measurement, agent_val)`: 5; reads `request.override_justification` and `request.relation_id`; the audit payload keys and values are unchanged (`source_id`, `target_id`, `predicate`, `violations`, `similarity`, `similarity_threshold`, `contradicting_predicates`, `justification`, `relation_id`).
- `_insert_relation_write(conn, request, canonical_predicate, repoint_stale_edge_id, note)`: 5; reads `request.valid_at`, `request.relation_id`, `request.now`.
- `_store_relation_write(conn, request)`: 2; the body is today's with `request.<field>` reads, the same order of steps (canonicalise, D1 no-op short-circuit, repoint detection, core guard, measure, apply gate, insert).
- `store_relation`: builds `_RelationWriteRequest(resolved_source, resolved_target, predicate, valid_at, override_justification, agent_id, relation_id, now, _allow_core_elaborates_on)` once, after `relation_id` and `now` are created, and passes it at both call sites (the direct call and the lambda).

## 3. Decisions

- D1. Behaviour is identical: the order of steps, every message text, every error code, the rollback behaviour of a gate rejection (a rejection returns and the transaction commits nothing for this call), the audit event written before the relation, the `ON CONFLICT` backstop and its no-op message, and the repoint invalidation that happens only after every gate passed.
- D2. No change to any SQL text or bound-parameter order, any threshold, or `_check_core_relation_guard`.
- D3. `bulk_store_relations` and other callers of `store_relation` are not touched; they keep calling `store_relation` with its unchanged signature.
- D4. No `noqa`, no new import except `dataclass` (already imported at line 8).
- D5. The gate tests patch module attributes by string (`RELATION_GATE_MIN_SIMILARITY_THRESHOLD`, `min_pairwise_cohesion`, in `TestStoreRelationBoundaries`, `tests/test_relation_service.py` 4139 to 4150). The new code must read those names from the module at call time: no threshold or function bound at definition time (no dataclass field default, no default argument, no alias in a class body).

## 4. Tests (characterization, green before and after)

The gate is already pinned exactly. About 30 tests in `tests/test_relation_service.py` (around lines 2586 to 2870) cover override stores and audits, weak predicate bypass, alias canonicalisation, unresolved entity, duplicate no-op and the per-item bulk gate, and the class `TestStoreRelationBoundaries` (line 3801, module prefix at 3807) pins: the low-similarity message at four decimals (4138 to 4161), the unresolved suffix (4163 to 4181), the contradiction message (4183 to 4204), the code and order when both violations are present (4328 to 4351), the audit payload on the override paths (four tests, 4206 to 4326), the repoint after the gate (4353), the rollback before the repoint (4387) and the `ON CONFLICT` backstop (4418). No new test is required. The developer re-reads those tests, confirms they pass before and after, and adds a test only for a behaviour of the five helpers that none of them asserts (expected: none); a duplicate of an existing assertion is not wanted.

## 5. Out of scope

`invalidate_relation` and the other BL-064 slices; the 11-parameter `store_relation`; the consolidation chain; changing the gate's rules.

## 6. Acceptance

1. `./verify` exits 0 (judge: exit status and pytest counts).
2. The section 4 tests pass on the unchanged code and after the change (the developer reports both runs; the architect reruns on the final tree).
3. `git status --short` lists `relation_service.py` and, only if section 4 added a test, `tests/test_relation_service.py` (judge: architect).
4. `rg -n "sim, offending, unresolved, contradictions = " src` returns nothing, and the parameter names `resolved_source`, `resolved_target` and `override_justification` appear in none of the signatures of the five helpers (judge: the architect reads the five definitions).
5. Mutation check on a scratch copy, killer named for each: swap `similarity` and the threshold in the comparison; drop `"contradictory_predicate"` from the violations; format the similarity with another precision; write the audit event after the relation; drop the repoint guard; read `request.predicate` instead of the canonical one in the gate; swap `source_id` and `target_id` in the measurement query; omit `relation_id` from the audit payload. A surviving mutant is reported.
6. Design read (architect and consultant): at most 5 parameters per function, no splatted tuple, one responsibility per function.

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| Parameter counts: `_apply_relation_gate` 11, `_relation_gate_rejection` 6, `_relation_gate_measurements` 4, `_insert_relation_write` 9, `_store_relation_write` 10 | `relation_service.py` 194, 228, 293, 370, 436 | read; scout AST scan |
| Each helper has exactly one caller in src and none in tests, scripts or hooks | `rg` of the five names over src tests scripts hooks | probed (only `relation_service.py` hits: 254, 501, 505, 521, 708, 723) |
| `_store_relation_write` is called at two sites with the same ten values | `relation_service.py` 708, 723 | read |
| `store_relation` has 118 test calls | scout AST scan | probed by the scout |
| The two rejection codes and the audit type appear in tests | `rg` count: 13, 4, 12 | probed |
| No test patches any of the five helper names (private patches exist only for `_resolve_relation_endpoint`, `_consolidation_insert_lineage` and `_resolve_and_filter_parent_ids`) | `rg` over tests | probed by the consultant |
| The project's S107 limit is 13 | Sonar issue text on `develop` | read |
