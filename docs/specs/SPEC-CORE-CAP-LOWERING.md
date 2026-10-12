# SPEC-CORE-CAP-LOWERING

## 0. Status

DRAFT (2026-10-12). Not assigned: the owner postponed implementation to Wednesday 2026-10-14. The
pre-lock gate (section 9) is partly done; the items still open are listed there.

- Backlog item: BL-045. Shared `context_id`: `bootstrap-output-revision-2026-10-12`. Plan:
  `docs/specs/PLAN-BOOTSTRAP-OUTPUT-REVISION.md`.
- Baseline: `develop` at 5f01fbf (tree clean). Record the `./verify` result counts at lock time;
  the previous slice (BL-044) recorded 2249 passed at 83c5450.
- Location: main checkout; the branch is named in the assignment.
- Test seams: the constants in `saltmdb.config`; `core_governance_service.find_invariant_violations`,
  `render_bootstrap_response`, `render_bootstrap_error`; the capacity gate reached through
  `store_memory(..., db_connection=...)`.
- Scope (may edit):
  - `src/saltmdb/config.py` L754 and L765-767
  - `src/saltmdb/mcp/tools.py` L504 (tool description text)
  - `docs/architecture.md` L235
  - `tests/test_core_governance_service.py` (the test at L1366-L1450, the comments at L254, L1369 and
    L1374, and new tests)
- Does not touch: `hooks/`, `src/saltmdb/domain/services/session_digest_service.py`,
  `src/saltmdb/daemon/`, any `docs/specs/*` file other than this one (older specs keep the old
  figure as history), `docs/BACKLOG.md` (the architect edits it), anything outside the repository.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The bootstrap hook output is capped at 9,800 characters (`HOOK_OUTPUT_MAX_CHARS`,
`hooks/_saltmdb_hook_common.py` L299). The core digest may legally render up to 15,000 characters
(`CORE_MAX_RENDERED_CHARS`, `config.py` L754), and the bootstrap error report up to 12,000
(`CORE_BOOTSTRAP_ERROR_MAX_CHARS`, `config.py` L765). Both are above the whole hook limit, so a
legal core set can fill the output and push out the session handover, which the previous slice
exists to deliver. The comment at `config.py` L764-767 states the intent that the error report can
never trigger the hook's truncation; at 12,000 that is no longer true.

Budget arithmetic (estimates; the frame is defined in the follow-up structure spec): 9,800 minus
the 3,500-character session-digest floor (`SESSION_DIGEST_MIN_BUDGET`, `_saltmdb_hook_common.py`
L303), the 450-character cut notice (`NOTICE_RESERVE_CHARS`, L302) and the output frame leaves about
5,000 for the core digest.

Measured on 2026-10-12: the live core digest is the empty block (36 characters printed), so no
installed core set is affected here. The cap is enforced in two places, at write time
(`check_capacity_admission`, L648-L705) and at render time
(`find_invariant_violations`, L624), so a larger existing set would fail closed into the bounded
error report at the next bootstrap. That failure mode is unchanged by this spec.

Probe done for this draft (no repository edit): the constants were patched to 5,000 and 4,000 from
a scratch runner and the 17 Python test files that touch the core digest were run. One test failed,
`TestEffectiveMemoryTypeGovernanceSizing::test_omitted_type_capacity_admission_uses_preserved_type_not_fact`
(section 3.4); the other 16 files passed.

Rejected: letting the handover shrink to zero (contradicts the continuity goal); leaving the cap and
relying on the hook's tail cut (status quo: the whole session section is cut first); lowering only
the error cap.

## 2. Decisions

- D1. `CORE_MAX_RENDERED_CHARS` becomes 5000.
- D2. `CORE_BOOTSTRAP_ERROR_MAX_CHARS` becomes 4000, the same 80% ratio to the digest cap as before
  (12,000 of 15,000).
- D3. `CORE_MAX_ACTIVE` (5) and `CORE_MAX_CONTENT_CHARS` (2,500) do not change. Five maximum-content
  cores already exceeded the rendered cap; the rendered cap stays the binding limit.
- D4. No data migration. If the implementer's re-measurement of the live set (section 8, step 1)
  exceeds 5,000 rendered characters, stop and report to the architect; do not alter data.
- D5. The hook constants are not changed here. Worst case until the structure spec lands: 5,000
  (core) + 1,400 (overdue reserve) + 3,500 (session floor) + 450 (notice) = 10,350, above 9,800, so
  the existing tail cut can still fire with a full core set and an overdue notice. Accepted as an
  interim state; the structure spec folds the overdue notice into the core section and removes the
  1,400 reserve.
- D6. The same figures appear in the owner's agent instruction text, which lives outside this
  repository. Updating it is an owner step (section 5), not part of this diff.

## 3. Changes per file

### 3.1 `src/saltmdb/config.py`

L754: `CORE_MAX_RENDERED_CHARS = 5000` (comment unchanged except it may add: sized to the hook
output limit). L765: `CORE_BOOTSTRAP_ERROR_MAX_CHARS = 4000`. Update the comment at L765-767 so it
still says the error report stays comfortably below the digest cap and below the hook limit; the
text must not contain the old figures.

### 3.2 `src/saltmdb/mcp/tools.py`

L504: replace `a 15,000-char rendered bootstrap digest` with `a 5,000-char rendered bootstrap
digest`. Re-grep the old string across the whole tree (section 9): the only other occurrence is in a
locked historical spec, which stays as is.

### 3.3 `docs/architecture.md`

L235: `≤ 15,000` becomes `≤ 5,000`. Add one sentence below the table: the digest cap is sized so that
the core digest, the session digest floor and the output frame fit the hook output limit. Do not
copy any memory text into the doc.

### 3.4 `tests/test_core_governance_service.py`

- L254, L1369, L1374 are comments that quote 15,000 or 15000 and the numbers 14989 and 15001; update
  them to the new cap and the new boundary numbers.
- `test_omitted_type_capacity_admission_uses_preserved_type_not_fact` (L1366-L1450) is hand-tuned to
  the old cap: four filler cores with 500-character reason and exit text plus a target cannot be
  stored under 5,000 (its setup assertion at L1405 fails). Retune its fixture lengths so the
  boundary property still holds: the digest sized with the target's type miscounted as `fact` lands
  strictly under `CORE_MAX_RENDERED_CHARS`, and sized with the preserved type `preference` lands
  strictly over it, by the same small margins as before (11 under, 1 over). Measure the fixture once
  and hard-code the numbers; do not compute them with the code under test. Fewer fillers or shorter
  reason and exit text are both acceptable.

## 4. Tests

Written failing first (against the old constants).

- T1. `saltmdb.config.CORE_MAX_RENDERED_CHARS == 5000` and `CORE_BOOTSTRAP_ERROR_MAX_CHARS == 4000`
  as literals.
- T2. Worked example. A row with id of 36 characters, title of 60 characters, `core_reason` and
  `core_exit_condition` of 200 characters each, `full_content` of 2,500 characters and a future
  review date renders, through `render_bootstrap_digest`, to 3,280 characters at this baseline; two
  such rows render to 6,496. Assert `find_invariant_violations` reports no rendered-size violation
  for one row and one containing `exceeds CORE_MAX_RENDERED_CHARS=5000` for two. Both numbers were
  measured on 2026-10-12 at 5f01fbf; the structure spec re-measures them.
- T3. Capacity gate. Storing a second maximum-content core when one is already active is rejected
  with `CORE_CAPACITY_EXCEEDED`, `violated_dimensions` containing `rendered_size`,
  `limits.max_rendered_chars == 5000`, and `required_reduction.rendered_chars` equal to the measured
  proposed total minus 5,000 (measure once, hard-code).
- T4. The retuned boundary test of section 3.4.
- T5. The existing bounded-error-report tests (`TestBoundedBootstrapErrorReport`) keep passing; they
  already assert against `CORE_BOOTSTRAP_ERROR_MAX_CHARS` by name, so no edit is expected.

## 5. Documentation

`docs/architecture.md` as in section 3.3. The tool description change in section 3.2 is the
user-facing doc. Outside this repository, and for the owner: the instruction text that states the
15,000-character cap lives in the shared agent instruction source; update it and re-render. A
mismatch there does not break anything but leaves agents with the wrong number.

## 6. Verification procedure

`./verify` only. Do not start a parallel full run on the shared host.

## 7. Out of scope

- Any hook change, including the overdue reserve and the session floor (structure spec).
- Changing `CORE_MAX_ACTIVE`, `CORE_MAX_CONTENT_CHARS`, the reason and exit bounds or the review
  schedule.
- Renaming tags or changing the digest layout (structure spec).
- Editing historical specs, migrating or demoting existing cores, and the owner's instruction text.

## 8. Acceptance

Run in this order from the repository root with the repo `.venv`.

1. Before any edit: `git rev-parse --short HEAD` and `git status --short` (clean). Measure the live
   digest through the sanctioned CLI and record the result: `saltmdb-cli bootstrap-digest | wc -c`
   (expected 36, the empty block; more than 5,000 means stop, see D4).
2. Add T1-T4 and confirm the red phase: `.venv/bin/python -m pytest tests/test_core_governance_service.py -q`
   fails on the new tests and passes the old ones. T5 and the retuned test's old form are expected
   red or green as described in section 3.4; name any test that passes on the old code in the report.
3. After the change: the same command passes in full.
4. `rg -n "\b15,?000\b" src docs/architecture.md tests` prints nothing.
5. `rg -n "CORE_MAX_RENDERED_CHARS = |CORE_BOOTSTRAP_ERROR_MAX_CHARS = " src/saltmdb/config.py` shows 5000
   and 4000.
6. `./verify` exits 0; record the pass count against the baseline count.
7. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

Done for this draft:
- Probe (section 1): patched constants, 17 core-related test files run, one failure identified.
- Live measurement: 36 characters (empty block).
- Old-text grep: `15,000` and `15000` found in `config.py`, `mcp/tools.py`, `docs/architecture.md`,
  `tests/test_core_governance_service.py` (comments) and, as history, two locked specs
  (`SPEC-TOOL-DESCRIPTION-REWRITE.md` L325 and L398, `SPEC-MEMORY-CONTENT-LENGTH-CAP.md` L40 and
  L43). No test asserts the tool description text; `rg` for the figure in `tests/` finds only the
  comments listed.
- Worked example numbers in T2 measured by running the renderer.

Still open before `LOCKED`:
- Run `./verify` at the baseline and record counts.
- Confirm the exact current line numbers once more at lock (the numbers above are from 5f01fbf).
- Review by a read-only consultant, with the question "what would make a developer report BLOCKED
  or build the wrong thing?"; then re-run the same-fact check between sections 2, 3 and 4 and
  after any review-driven change grep the whole spec for the old and the new wording.
- Decide whether D5's interim state is acceptable or the two slices must land together.
