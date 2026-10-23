# SPEC-BL064-S1-DEPENDENCY-GRAPH

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 1 (the first item of its stated order). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `9103f6d`; `./verify` exits 0 at `9103f6d` (3620 passed, 10 skipped, 391 subtests passed). Targeted baseline at `ade5bdf`: `.venv/bin/python -m pytest tests/test_relation_service.py tests/test_bitemporal_relations.py -q` gives 191 passed, 32 subtests passed.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `analyze_dependencies` and `get_related_memories` (public service functions in `relation_service.py`); no test calls the private helpers.
- Scope (may edit or create): `src/saltmdb/domain/services/relation_service.py` (lines 1118 to 1231, the call at 1316, and the `dataclasses` import on line 8, which gains `field`), `tests/test_relation_service.py` (new characterization tests only, appended to `TestRelationPointInTime`).
- Does not touch: the SQL builders `_dependency_cte_sql` and `_fetch_dependency_rows`, `analyze_dependencies` and `get_related_memories` signatures and docstrings, any other module, any existing test.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`_append_dependency_row` takes ten parameters, four of them mutable accumulators (`seen_nodes`, `seen_edges`, `nodes`, `edges`) that its caller creates and threads through the loop, and `_build_dependency_result` takes eight. The signatures were shaped by the earlier complexity refactor, not by a domain concept. The state that travels together is the partially built graph of one walk. A small named type that owns that state removes the out-parameter accumulators and gives the node-inspection call, which is written out twice (once for the root, once per reached node), a single home. Neither function is flagged by Sonar today (the limit is 13); this slice is the quality half of BL-064, and it is the smallest, so it also fixes the pattern for the slices after it.

Rejected: passing a dict or tuple of the accumulators (a bag, not a type); a module-level helper that returns four lists (an out-parameter in disguise); changing the SQL or the row order (behaviour change); renaming the public `include_inspect` argument (v2 guard territory, not this slice).

## 2. Target shape

One private class in `relation_service.py`, placed where `_append_dependency_row` is today:

```python
@dataclass(slots=True)
class _DependencyGraph:
    """The graph one dependency walk builds: root first, then nodes and edges in discovery order."""

    conn: sqlite3.Connection
    root_info: dict
    include_inspect: bool
    max_depth: int
    agent_id: str | None
    nodes: list[dict] = field(init=False, default_factory=list)
    edges: list[dict] = field(init=False, default_factory=list)
    seen_nodes: set[str] = field(init=False, default_factory=set)
    seen_edges: set[str] = field(init=False, default_factory=set)
```

(`field` is imported with `dataclass` from `dataclasses`: extend the existing import on line 8. The four accumulators are `init=False` so a caller can never inject foreign state; `start` seeds them.)

Responsibilities, each one function:

- `@classmethod start(cls, conn, root_id, root_info, include_inspect, max_depth, agent_id) -> _DependencyGraph`: builds the root node `{"id": root_id, "title": root_info.get("title"), "depth": 0}`, adds the inspection fields when `include_inspect` is true, and seeds `seen_nodes` with `root_id`.
- `_inspect(self, memory_id: str) -> dict | None`: the one place that calls `_assemble_memory_record(self.conn, memory_id, include_content=False, include_lineage=False, touch=False, max_depth=self.max_depth, agent_id=self.agent_id)`, with the same lazy import inside the method that the code has today (the import is lazy now; do not hoist it).
- `add_row(self, direction, row) -> None`: the body of `_append_dependency_row`, reading the walk options from `self`.
- `add_rows(self, tagged_rows) -> None`: sorts `tagged_rows` by depth (`item[1][6]`, stable, in place on the list it is given, as `_build_dependency_result` does today) and calls `add_row` for each.
- `result(self, point_in_time: str) -> dict`: returns the response dict with exactly the keys and order of today (`root`, `total_dependencies_found`, `graph_exhausted`, `dependencies`, `edges`, `point_in_time`) and the same `graph_exhausted` expression.

`analyze_dependencies` (line 1315 to 1318) becomes, in this order (today the rows are fetched before the root is inspected; keep that order so the same exception wins when both fail): `rows = _fetch_dependency_rows(conn, direction, root_id, pit, max_depth, agent_id)`, then `graph = _DependencyGraph.start(conn, root_id, root_info, include_inspect, max_depth, agent_id)`, then `graph.add_rows(rows)`, then `return graph.result(pit)`. `_append_dependency_row` and `_build_dependency_result` are deleted. Every function after the change has at most 6 parameters (`start`, besides `cls`).

## 3. Decisions

- D1. Behaviour is identical: same nodes and edges, same order, same dict keys and key order at the top level and inside every node (a node starts as `{id, title, depth}` and is merged with `node.update(inspected)` exactly as today: the three existing keys keep their position and may be overwritten by the inspected values, the other inspected keys follow; do not use `{**inspected, **node}` or a "fill missing" merge), same depth values, same dedupe rules (a node by id, an edge by relation id, shallowest first), same handling of an `include_inspect` lookup that returns `None` (the node stays as `{id, title, depth}`), same exception behaviour (an exception inside the walk still reaches the `except Exception` in `analyze_dependencies` and becomes `{"error": ...}`).
- D2. The `Literal["outbound", "inbound"]` direction logic is unchanged: for an outbound row the reached node is the target, for an inbound row it is the source. Keep the existing comment.
- D3. No new third-party or cross-module import; `sqlite3` and `dataclass` are already imported in the file (lines 5 and 8). `conn` was unannotated before; annotate it `sqlite3.Connection` and run mypy.
- D4. Tests first (section 5), green on the old code, then the refactor; the same tests are green after.
- D5. No `noqa`, no change to any threshold or Sonar setting.

## 4. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/domain/services/relation_service.py` | Add `_DependencyGraph`; delete `_append_dependency_row` (1118) and `_build_dependency_result` (1176); update the call in `analyze_dependencies` (1315 to 1318). |
| `tests/test_relation_service.py` | New characterization tests (section 5). |

## 5. Tests (characterization, green before and after)

Gap found at lock time: no test passes `include_inspect=True` to `analyze_dependencies` or `get_related_memories` at the service level (the only mention is a guard-rename test in `tests/test_mcp_graph_tools_v2.py` that is rejected before the service runs). Add them to the `unittest.TestCase` class `TestRelationPointInTime` (`tests/test_relation_service.py`, `setUp` at 1553, the existing `test_analyze_dependencies_*` tests at 1575 to 1777), reusing its `setUp` and helpers; because it is a `TestCase`, use `unittest.mock.patch`, not the pytest `monkeypatch` fixture. Expected values: literal key SETS for the inspected fields (they include ids and timestamps that change on every run), and literal values only for `id`, `title`, `depth`, `predicate`, node order and counts, all taken from a run on the unchanged code:

1. `include_inspect=True`, outbound, a root with two reachable nodes: the root node and each reached node carry the inspection fields (the exact key set), `include_content`-style fields are absent, and the node order matches the discovery order.
2. `include_inspect=True` with `direction="both"` on a graph with a diamond: a node reached twice is inspected once, and the edge list has no duplicates. Observe the inspect calls with `unittest.mock.patch("saltmdb.domain.services.memory_service.lifecycle._assemble_memory_record", wraps=<the real function>)` (the import is lazy inside the call, so the patch works before and after the refactor), and assert the call count per id.
3. An inspection that returns `None` for one reached node: patch the same attribute with a wrapper that returns `None` for one chosen id and delegates to the real function for every other id; that node appears as `{id, title, depth}` only and the others are inspected. This is the only way to reach the `None` branch: `_dependency_cte_sql` already filters nodes the `agent_id` may not read before any row exists, and `_assemble_memory_record` returns `None` only when the entity row is missing (a concurrent-delete race). Do not change the SQL to make the case reachable.
4. The top-level key order of the result: `list(result)` equals `["root", "total_dependencies_found", "graph_exhausted", "dependencies", "edges", "point_in_time"]`.
5. `graph_exhausted` is true when no edge reaches `max_depth` and false when one does (the existing depth tests may already pin one side; the developer lists which and adds only the missing side).

## 6. Out of scope

The other BL-064 slices (gate measurement tuples, community collector, `_replacement_prepare`/`_replacement_fields`, search channel objects, store request objects, consolidation helpers, `_publish_ready`), the `ruff noqa C901/PLR09xx` question, the SQL, any rename of `include_inspect`.

## 7. Acceptance

1. `./verify` exits 0 (judge: exit status and the pytest counts).
2. The new tests of section 5 pass on the unchanged code and after the change (judge: the developer reports both runs; the architect reruns on the final tree).
3. `rg -n "_append_dependency_row|_build_dependency_result" src tests hooks scripts` returns nothing.
4. `git status --short` lists only the two files of section 0 (judge: architect).
5. Mutation check on a scratch copy, tests named as killers: swap outbound/inbound reached-node selection; drop the `seen_edges` dedupe; drop the `seen_nodes` dedupe; skip `_inspect` for the root; skip `_inspect` for reached nodes; replace the depth sort with no sort; change `graph_exhausted` comparison; build the result keys in another order. A surviving mutant is reported.
6. Design read (architect, with the consultant): one responsibility per method, at most 6 parameters, no state bag, no duplicated inspection call.

## 8. Claims

| Claim | Where | Verified |
|---|---|---|
| `_append_dependency_row` has 10 parameters, `_build_dependency_result` 8, one caller each | `relation_service.py` 1118, 1176, 1211, 1316 | read; `rg` over src, tests, hooks, scripts, docs: only these hits |
| No test calls the two helpers | same `rg` | probed |
| 191 tests pass in the two relation test files | pytest run at `ade5bdf` | probed |
| No service-level test uses `include_inspect=True` for dependencies | `rg include_inspect tests` | probed (one guard test only) |
| The `_assemble_memory_record` import is lazy | `relation_service.py` 1139 and 1192 | read; the reason is the lazy import of `relation_service` at `lifecycle.py` 104; not re-verified that hoisting would fail, hence "do not hoist" |
| `sqlite3` and `dataclass` are imported at file top; `field` is not | `relation_service.py` 5, 8 | read |
| The project's S107 limit is 13 | Sonar issue text on `develop`: "greater than the 13 authorized" | read (Sonar tool) |
| `_dependency_cte_sql` filters rows by `agent_id`/shared visibility on both endpoints; `_assemble_memory_record` returns `None` only for a missing entity row | `relation_service.py` ~1076-1083; `memory_service/lifecycle.py` | consultant read |
| `TestRelationPointInTime` is the `TestCase` holding the dependency tests | `tests/test_relation_service.py` 1553-1777 | consultant read |
| Key order of the result dict | `relation_service.py` 1222 to 1229 | read |
