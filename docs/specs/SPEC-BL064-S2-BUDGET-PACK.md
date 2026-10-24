# SPEC-BL064-S2-BUDGET-PACK

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 2 (budget tier tuples). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `ae977ae` or later; `./verify` exits 0 at `ae977ae` (3632 passed, 10 skipped, 391 subtests passed; one run in two showed the unrelated teardown flake of BL-083, rerun once). `.venv/bin/python -m pytest tests/test_context_budget_service.py -q` is the targeted baseline (12 scenarios in one `TestCase`).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `pack_context_budget` (public; 16 test calls: 13 in `tests/test_context_budget_service.py`, 2 in `tests/test_orphan_community_service.py`, 1 in `tests/test_retrieve_context_service.py`; 2 src callers in `retrieve_context_service.py`). `_pack_ids` and `_budget_output` have no direct test.
- Scope (may edit): `src/saltmdb/domain/services/context_budget_service.py` (lines 42 to 55 and 72 to 116 and the body of `pack_context_budget` from the `used = 0` line, plus the imports), `tests/test_context_budget_service.py` (new characterization tests only).
- Does not touch: `pack_context_budget`'s signature, docstring and return shape, `_empty_budget_result`, `_load_token_counts`, `_open_budget_connection`, `retrieve_context_service.py`, any other test.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`_pack_ids(ids, token_counts, used, budget)` returns `(packed, dropped, used)` and `pack_context_budget` threads `used` through three calls, rebinding it each time: a running total passed in and returned out. `_budget_output` then takes twelve parameters: three packed/dropped pairs, three reserve id sets, the token counts, the final `used` and the limit. The state that belongs together is the ledger of one packing pass (the token counts, the limit, the running total) and the result of packing one section (what fit and what was dropped). Neither function is flagged by Sonar today (limit 13); this is the quality half of BL-064.

The three new value types hold lists or sets: add the one-line comment `# not hashable: carries a list/set` on each, as `ReplacementRequest` does. `_budget_output` puts the `_Section` lists into the result as the same list objects, as today.

Rejected: a dict of sections (a bag); a `NamedTuple` result for `_pack_ids` only (the running total would still be threaded); changing `pack_context_budget`'s public signature or output (its keys and order are consumed by `retrieve_context_service` and pinned by tests).

## 2. Target shape

All private, in `context_budget_service.py`, defined where `_pack_ids` and `_budget_output` are today:

```python
@dataclass(frozen=True, slots=True)
class _Section:
    """What one packing pass kept and what it dropped, in input order."""
    packed: list[str]
    dropped: list[str]

@dataclass(frozen=True, slots=True)
class _ReserveIds:
    """Ids charged to a reserve outside the ordinary budget."""
    conflict: set[str]
    orphan: set[str]
    representative: set[str]

@dataclass(slots=True)
class _BudgetLedger:
    """Token costs, the limit and the running total of one packing pass."""
    token_counts: dict[str, int]
    limit: int
    used: int = 0

    def pack(self, ids: list[str]) -> _Section: ...
```

- `_BudgetLedger.pack(self, ids)`: the body of today's `_pack_ids`, reading `self.token_counts` and `self.limit`, adding each packed cost to `self.used`, returning a `_Section`. Same first-fit rule: an id that does not fit is dropped and the loop continues with the next id (scenario 4 pins it); the same `token_counts[entity_id]` lookup (a miss is unreachable: `_load_token_counts` builds a count for every id of the union of the six id groups, so write no test or mutant around a missing count).
- `_budget_output(ledger: _BudgetLedger, primary: _Section, expansion: _Section, members: _Section, reserves: _ReserveIds) -> dict[str, Any]`: 5 parameters; reads `ledger.token_counts`, `ledger.used` and `ledger.limit`; builds exactly today's dict (same keys, same order, `sorted(...)` of the conflict and representative sets, `sum(...)` over the reserve ids).
- `pack_context_budget`: after `token_counts = _load_token_counts(...)`, `ledger = _BudgetLedger(token_counts, effective_budget)`, then `primary = ledger.pack(primary_ids)`, `expansion = ledger.pack(expansion_ids)`, `members = ledger.pack(member_ids)` in that order, then `return _budget_output(ledger, primary, expansion, members, _ReserveIds(conflict_only_ids, orphan_ids, representative_ids))`.
- `_pack_ids` is deleted. `from dataclasses import dataclass` is added to the imports.

Hand count: `pack` 2 (with self), `_budget_output` 5, `pack_context_budget` unchanged at 9.

## 3. Decisions

- D1. Behaviour is identical for every input, including: the order of the three `pack` calls (the running total makes order significant), packed and dropped lists in input order, the reserve sums being computed over the reserve sets (not over what packed), `sorted` on the two id sets only, the value of `used` equal to the sum of the packed costs, and the `finally` that closes the connection.
- D2. No change to `_empty_budget_result`, which has its own copy of the output key order; the new characterization test in section 4 pins both orders so they cannot drift.
- D3. No `noqa`, no change to any threshold; `./verify` stays clean (ruff, mypy, bandit).

## 4. Tests (characterization, green before and after)

The 12 existing scenarios already pin packing order, first-fit, clamp, reserves and the empty case. Add to `TestContextBudgetService`, reusing its fixtures, with literal expected values taken from a run on the unchanged code:

1. The key order of the result, which today's `assertEqual` on dicts does not pin: `list(result)`, `list(result["budget"])`, `list(result["packed_entity_ids"])` and `list(result["dropped_entity_ids"])`, each compared with one literal list, for one non-empty input and for the all-empty input (`_empty_budget_result`); the two shapes have the same key set at every level, so one literal list per level serves both.
2. A mixed run in which the budget is exhausted inside the expansion section, the member section then has one id that fits (first-fit continues across sections because `used` carries over) and one that does not, and conflict, orphan and representative ids are all non-empty: assert every packed/dropped list, `used`, the three `*_reserve_tokens_used` values and the two truncation flags.
3. `used` equals the sum of the token counts of every packed id (primary, expansion, members) and excludes the reserve ids.

If an existing scenario already asserts one of these exactly, the developer names it and skips the duplicate.

## 5. Out of scope

The other BL-064 slices; `_load_token_counts`; the budget defaults in `config.py`; `retrieve_context_service.py`.

## 6. Acceptance

1. `./verify` exits 0 (judge: exit status and pytest counts).
2. The section 4 tests pass on the unchanged code and after the change (judge: the developer reports both runs; the architect reruns on the final tree).
3. `rg -n "_pack_ids" src tests hooks scripts` returns nothing.
4. `git status --short` lists only the two files of section 0 (judge: architect).
5. Mutation check on a scratch copy, killer named for each: pack the three sections in another order; drop the `self.used +=`; make `pack` stop at the first drop; compute the reserve sums over packed ids; drop `sorted` on the conflict ids; swap `primary` and `expansion` in the output; change a `*_truncated` flag source. A surviving mutant is reported.
6. Design read (architect and consultant): one responsibility per type and method, at most 5 parameters, no state bag, no out-parameter.

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| `_pack_ids` has 4 parameters and is called 3 times from `pack_context_budget` | `context_budget_service.py` 42, 173, 176, 179 | read |
| `_budget_output` has 12 parameters and one caller | `context_budget_service.py` 72, 182 | read; scout AST scan |
| `pack_context_budget` has 9 parameters, 16 test calls and 2 src callers | scout AST scan | probed by the scout |
| `_pack_ids` and `_budget_output` have no direct test | `rg` over tests | probed by the scout |
| The existing test class has 12 scenarios including first-fit and reserves | `tests/test_context_budget_service.py` 92 to 357 | read (names) |
| The project's S107 limit is 13 | Sonar issue text on `develop` | read |
