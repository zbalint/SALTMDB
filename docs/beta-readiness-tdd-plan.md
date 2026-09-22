# Beta readiness fixes: TDD handoff

Prepared 2026-09-22 from read-only review of `develop` at `063c1a2`. This is a
plan, not a claim that the findings have been fixed. Recheck HEAD and the working
tree after a context clear before changing files. Never modify `master` without
zbalint's fresh, action-specific permission.

## Baseline and evidence

- Main suite: `.venv/bin/pytest -x -q tests` passed 1,724 tests and 18 subtests
  with local socket access. The sandboxed run failed on socket creation, which
  was an environment restriction.
- Hook suite: `.venv/bin/pytest -q hooks/tests` passed 18 tests but is excluded
  by the CI command `python -m pytest tests/`.
- Local release gates were red: `ruff check .` (3 complexity errors),
  `ruff format --check .` (20 files), `mypy src` (11 errors),
  `bandit -c pyproject.toml -r src -q` (3 Low findings), and `deptry src`
  (4 dependency issues). The current GitHub failure log was unavailable; do not
  assume which of these explains it. `pip-audit` and a build check were not run.
- Current-source security finding: `mcp/tools.py` sends owner identity on
  `get_memory`, but `daemon/dispatch.py::_dispatch_get_memory` discards it;
  `memory_service/lifecycle.py::get_memory` has no owner check. The same family
  of explicit-ID and graph retrieval tools requires an access audit. Search is
  already owner-filtered. See SALTMDB memory `1225cba9` (2026-09-21), then
  verify the current code again.
- CI triggers only on `master`/`main`; `INSTALL.md:229` and
  `CONTRIBUTING.md:20,81` still recommend `unittest` despite pytest-only tests.

## TDD agreement and working rules

Read the installed `tdd` and `systematic-debugging` skills at the start of the
fix session. The architect's chosen security seam is two configured MCP adapters
(different owner IDs) calling the public tools against one temporary DB.
zbalint's request to fix this plan after the context clear confirms that seam
for TDD; if he requests a different seam, update the plan first. For CI/test
discovery, the observable seam is the repository's
documented `verify` command and the GitHub workflow. Use a real temporary DB,
not mocks of internal services. Each cycle is one failing behavior test or gate,
the smallest implementation that makes it green, then the next cycle. Do not
batch all test cases before implementing. Record the red and green output.

## Work sequence

### 1. Fix private-memory disclosure before beta

1. Reproduce one red public-boundary case: owner A stores a private memory;
   owner B calls `get_memory` with its known full ID and must receive a
   non-disclosing rejection. Confirm owner A can still read it and both owners
   can read a shared memory. Use temporary identities/DB only.
2. Implement the minimum owner/scope check at the common retrieval boundary;
   carry the authenticated owner from the MCP adapter through dispatch to the
   service. Keep `owner_id` absent from public MCP tool parameters. Check both
   exact IDs and prefixes so rejection does not reveal private titles or
   candidate IDs. Rerun the focused red test, then existing retrieval tests.
3. Repeat vertical red/green slices for `inspect_memory`, `get_lineage`,
   `get_related_memories`, `retrieve_context(local)`, and
   `retrieve_context(global)`. Test both private anchors and private expansion
   neighbors, including metadata, lineage, conflicts, community membership,
   and pagination where returned. Audit other ID-addressed reads for the same
   owner-drop pattern; do not infer completeness from ACIE's graph alone.
4. Document the contract: inaccessible private IDs should use the same
   externally observable result as unknown IDs unless the existing API has a
   stronger, already-agreed privacy rule. Never expose a private ID/title in
   errors. Run the focused suite and full suite after the final slice.

### 2. Make all shipped tests part of the gate

1. Capture a red discovery check showing the workflow/`verify` command omits
   `hooks/tests/`. Update the test command to include both `tests/` and
   `hooks/tests/`, then verify the hook cases run in the combined suite.
2. Add `develop` to workflow triggers so integration-branch changes get CI.
   Verify the workflow YAML and event/branch filters with an appropriate
   workflow validation check; do not treat grep alone as a runtime CI result.
3. Align the documented command in `INSTALL.md` and `CONTRIBUTING.md` with
   pytest and the final CI command. Show that collection includes pytest-style
   function tests and the hook suite. Avoid a test that merely asserts the docs
   contain a string.

### 3. Clear the existing release gates, one failure class at a time

1. Preserve the focused behavior baseline for `get_relevance_preview_data`;
   address its Ruff C901/PLR0912/PLR0915 failure without changing preview
   selection, budget accounting, or the unverified-warning prefix. Re-run the
   relevant preview tests and `ruff check .`.
2. Run Ruff format on explicitly reviewed target files, inspect each diff for
   collateral changes, and re-run `ruff format --check .`. Formatting is a
   mechanical gate repair, so the check itself is its red/green signal.
3. Resolve all 11 mypy diagnostics at their actual boundaries. In particular,
   check nullability in preview calls and dispatch metadata, dependency-analysis
   kwargs, daemon capability comparison, and Windows-only ctypes typing.
   Add behavior tests only where a runtime contract changes. Re-run `mypy src`.
4. Inspect the three Bandit findings individually. Replace the silent catch in
   tag resolution with observable error handling; handle the two `assert`
   guards explicitly if they can affect optimized Python. Re-run Bandit; do not
   globally suppress the rules to turn the gate green.
5. Resolve the four deptry findings by checking import/package naming and
   direct dependency declarations (`igraph`/`python-igraph`, `anyio`,
   `typing_extensions`). Re-run deptry and a clean package install/build.

### 4. Verify launch readiness and report separately

Run the repository's final documented suite and every `verify` check fresh,
including `pip-audit` and build sanity in an environment with network access.
Check Python 3.10 as the declared minimum; CI currently exercises only Linux
Python 3.11. Inspect `git diff` and report all changed files, test counts,
remaining failures, and the exact GitHub CI run result once available. Recheck
the beta release wayfinder map (`cdaba427`) and its other open tickets,
including the duplicate-detection FTS fallback, before claiming beta-ready.
This plan does not authorize tagging, publishing, pushing, or changing `master`.

## Resume pointer

Read this file, the current source, and SALTMDB memories `335698b0` (gate/docs
review), `782326de` (omitted hook tests), and `1225cba9` (private scope), using
`get_memory` for any memory cited as fact. Search memory for newer decisions.
Begin at the public MCP security seam with one failing cross-owner test.
