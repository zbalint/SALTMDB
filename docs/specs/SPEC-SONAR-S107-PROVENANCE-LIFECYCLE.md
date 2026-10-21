# SPEC-SONAR-S107-PROVENANCE-LIFECYCLE

## 0. Status

LOCKED 2026-10-21 (consultant review folded in).

- Backlog item: none. Sonar `python:S107` (limit 13 parameters) on `develop` `39f48a4` or later: `revise_memory` (`src/saltmdb/domain/services/memory_service/lifecycle.py`, 15 parameters, issue lines 847-861) and `supersede_memory` (same file, 14 parameters, issue lines 890-903). Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Owner decisions: real fixes, no suppression, no Accept; the five service-layer functions are fixed by cohesive value objects, the public signatures may change, the MCP tool schema must not (the wire protocol and the MCP schema do not depend on service signatures: `mcp/tools.py` sends a flat params dict over the daemon protocol and `daemon/dispatch.py` calls the service with explicit keyword arguments). This is the first of a series (`Provenance` is reused by the later `consolidate_memories`, `store_memory` and `search_memory` slices).
- Baseline: `develop` at `4d34fa1` or later; `./verify` exits 0 (3213 passed or more, 1 skipped, 289 subtests; known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, `tests/test_daemon_server.py::TestDaemonSignalShutdown`; rerun a failing one alone once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the existing lifecycle suites (`tests/test_lifecycle_replacements.py`, `tests/test_lifecycle_replacements_characterization.py`, `tests/test_memory_service_characterization.py`, `tests/test_archived_embedding_status.py`, `tests/test_phase4_mcp_surface.py` service calls) and the dispatcher path (`daemon/dispatch.py` `_dispatch_replacement`).
- Scope (may edit): `src/saltmdb/domain/services/memory_service/_shared.py` (the new `Provenance` dataclass), `src/saltmdb/domain/services/memory_service/lifecycle.py` (the two public functions only), `src/saltmdb/domain/services/memory_service/__init__.py` (export `Provenance`), `src/saltmdb/daemon/dispatch.py` (`_dispatch_replacement` only), and EXACTLY these existing test edits: `tests/test_archived_embedding_status.py` (about lines 93-101, `revise_memory(..., agent_id="test_user", ...)`), `tests/test_lifecycle_replacements_characterization.py` (about lines 117-125, `replace(..., agent_id=..., context_id=...)`, and about line 363, `replace(..., agent_session_id="session-123")`; leave the `replace()` helper at lines 37-46 unchanged, it forwards overrides as is), and the ONE authorised assertion change in `tests/test_phase4_mcp_surface.py` (about lines 138-159, `test_revise_dispatch_calls_lifecycle_service`, whose `assert_called_once_with` lists `agent_id=None, context_id=None, agent_session_id=None` and must now list `provenance=Provenance(agent_id=None, context_id=None, agent_session_id=None)`; dataclass equality makes it exact); plus a NEW test file `tests/test_provenance.py`.
- Does not touch: `mcp/tools.py` (the MCP schema), `_ReplacementRequest` and `_replacement_operation` (they stay as they are: the public functions still build `_ReplacementRequest` with the three plain values), SQL, any assertion of an existing test, the other S107 functions.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`revise_memory` and `supersede_memory` take 15 and 14 parameters. The three provenance values travel together everywhere (`agent_id`, `context_id`, `agent_session_id`): who wrote it, in which task context and in which agent session. Grouping them into one value object is a domain-meaningful regrouping (the rule text asks for groups that make sense for the domain) and takes the functions to 13 and 12 parameters with about 20 test call sites to edit by hand. Rejected: `**kwargs` wrappers (hides the parameter list and loses signature checking); a flat request dataclass mirroring all fields (silences the rule without regrouping anything); raising `maxParameters` in the Sonar profile (owner-only, effectively a suppression); grouping the storage trio `db_connection`, `db_path`, `_in_transaction` instead (reaches 13 as well, but `db_connection=` appears in nearly every lifecycle test, so the churn is higher).

## 2. Decisions

- D1. In `memory_service/_shared.py` add `@dataclass(frozen=True, slots=True) class Provenance` with exactly three fields, all `str | None = None`: `agent_id`, `context_id`, `agent_session_id`. Export it from `memory_service/__init__.py` (import and `__all__`). All three are plain strings, so the class is hashable; no comment about hashing needed.
- D2. `revise_memory` and `supersede_memory`: remove the parameters `agent_id`, `context_id` and `agent_session_id`; add a trailing keyword-only parameter `provenance: Provenance | None = None` (after a bare `*`). Parameter counts become 13 (revise: the 12 remaining plus provenance) and 12 (supersede). The functions build `_ReplacementRequest(agent_id=..., context_id=..., agent_session_id=...)` from `provenance` (all `None` when `provenance` is `None`), so everything behind them is unchanged and the observable behaviour for equal inputs is identical. Update the docstrings with one sentence about `provenance`.
- D3. `daemon/dispatch.py` `_dispatch_replacement`: build `Provenance(agent_id=kw.get("agent_id"), context_id=kw.get("context_id"), agent_session_id=kw.get("agent_session_id"))` and pass `provenance=` instead of the three keywords; nothing else changes (the wire params, validation and error payloads stay byte-identical).
- D4. The consultant's audit (every service call in `src`, `hooks`, `scripts` and `tests`; all calls pass arguments by keyword, nothing is positional, and no other source caller exists besides `_dispatch_replacement`) found exactly three call sites that pass provenance keywords plus one mock assertion; the listed edits of section 0 are the whole set: each `agent_id=X, context_id=Y, agent_session_id=Z` among a call's keyword arguments becomes `provenance=Provenance(...)` with only the fields that call passed (for the two `replace(...)` override sites pass `provenance=Provenance(...)` as the override). Calls in other tests that pass none of the three keywords (for example in `tests/test_lifecycle_replacements.py`, `tests/test_memory_service_characterization.py`, `tests/test_phase4_mcp_surface.py` lines 99 and 116) are NOT edited. The `store_memory` call in a fixture that also passes `agent_id`/`context_id` is not touched. Still grep for positional calls as a guard (it should come back empty) and for any further call that passes the removed keywords; a further site is reported and fixed under the same rule. The `git diff -U0 -- tests | grep assert` sweep must show exactly ONE changed assertion: the `test_phase4_mcp_surface.py` revise dispatch assertion above.
- D5. New test file `tests/test_provenance.py` (literal expected values): `Provenance()` defaults to three `None`; it is frozen (assignment raises) and hashable; `revise_memory` and `supersede_memory` stored rows carry the `agent_id`, `context_id` and `agent_session_id` given through `provenance` (use the helper idiom of the lifecycle tests with a temporary DB; use three DISTINCT values so that swapping two fields or dropping one is caught), and when `provenance` is omitted the replacement INHERITS `agent_id` and `context_id` from the predecessor (the existing characterization pins this; write the literal expected values from a baseline run, including the `agent_session_id` behaviour as it is today), a case with only `agent_id` set asserts `context_id` is inherited, `agent_session_id` stays `None` (today's behaviour, measured by the developer: the session id is not inherited) and nothing is swapped, and the old keyword names are rejected (`TypeError` when `agent_id=` is passed directly); `inspect.signature` of both functions has at most 13 parameters; the dispatcher round trip is covered for BOTH `revise_memory` and `supersede_memory` (`_dispatch_replacement` with `agent_id`, `context_id`, `agent_session_id` in the params stores the same three distinct values).
- D6. Complexity and literals: no function's cognitive complexity rises; report complexipy before/after for the two functions and `_dispatch_replacement`. No suppressions, no `NOSONAR`. `ruff check --select ARG,F,RET` clean on every touched file (no unused arguments in tests).

## 3. Changes per file

| File | Change |
|---|---|
| `memory_service/_shared.py` | `Provenance` (D1) |
| `memory_service/__init__.py` | export (D1) |
| `memory_service/lifecycle.py` | the two public signatures and bodies (D2) |
| `daemon/dispatch.py` | `_dispatch_replacement` (D3) |
| existing tests | mechanical call-site regrouping (D4) |
| `tests/test_provenance.py` | new (D5) |

## 4. Tests

D4 and D5. Mutation sample (tester, scratch copy, import-pinned): ignore `provenance` (all three stay `None`); swap `context_id` and `agent_session_id`; read `provenance` only for `agent_id`; drop one field from the dispatcher; accept the old keywords again; make the dataclass mutable; drop the `*` or reorder so that `provenance` is positional. Each fails a test (a shared default `Provenance()` is an equivalent mutant because the class is frozen; do not chase it).

## 5. Documentation

`docs/architecture.md` or any doc that quotes the signatures of `revise_memory` or `supersede_memory`: update only an exact quotation.

## 6. Verification procedure

`./verify`; `tests/test_provenance.py` alone; `ruff check --select ARG,F,RET` on the touched files against the baseline; complexipy (scratch venv) on the touched functions; `rg -n "revise_memory\(|supersede_memory\(" src hooks scripts tests` showing no remaining call that passes the removed keywords to the SERVICE functions.

## 7. Out of scope

`consolidate_memories`, `store_memory`, `search_memory` (later slices that reuse `Provenance`), the two `mcp/tools.py` functions (their parameters are published MCP fields; the owner decides on a schema change), the Sonar quality profile.

## 8. Acceptance

1. `./verify` exits 0. 2. `git status` lists only files named in sections 0 and 3. 3. The edit list of section 0 and a removed-assert diff containing only the `test_phase4_mcp_surface.py` revise dispatch assertion. 4. Mutation sample of section 4. 5. Architect after CI and the Sonar scan: the two S107 issues closed.

Amendment 1 (2026-10-21, developer FYI m_2905, confirmed by the architect): a replacement inherits `agent_id` and `context_id` from the predecessor but NOT `agent_session_id`, which stays `None`; D5 pins that current behaviour and the partial-provenance case is worded accordingly.
