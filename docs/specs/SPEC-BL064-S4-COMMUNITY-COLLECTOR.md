# SPEC-BL064-S4-COMMUNITY-COLLECTOR

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 4 (community collector). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `ae977ae` or later; `./verify` exits 0 at `ae977ae` (3632 passed, 10 skipped, 391 subtests passed; one run in two showed the unrelated teardown flake of BL-083, rerun once). Targeted baseline: `.venv/bin/python -m pytest tests/test_community_detection_service.py tests/test_orphan_community_service.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `recompute_communities` (public) and `_compute_community_group` (an existing internal seam with one direct test, which this slice leaves alone). `_process_partition_group` has no direct test.
- Scope (may edit): `src/saltmdb/domain/services/community_detection_service.py` (`_process_partition_group`, lines 105 to 157, the collector use inside `recompute_communities`, lines 238 to 304, the one docstring sentence in `fetch_leaf_community_centroids` at line 301 that names `_process_partition_group`, and the imports), `src/saltmdb/config.py` (the comment at line 342 that names `_process_partition_group`, comment text only), `tests/test_community_detection_service.py` (new characterization tests only).
- Does not touch: `_compute_community_group`, `_community_group_inputs`, `_community_centroid`, `_community_representative`, `fetch_leaf_community_centroids` (except the docstring sentence named above), `trigger_community_detection`, the SQL text of the write, the `# noqa: C901, PLR0912, PLR0915` on `recompute_communities` (a complexity suppression; see section 5), any config constant or other line of `config.py`, any other module.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`_process_partition_group` takes ten parameters, three of them mutable row lists (`communities_to_insert`, `membership_to_insert`, `embeddings_to_insert`) that `recompute_communities` creates, passes into every recursive call, and later reads back to write the rows and to count the results. It carries a `# noqa: PLR0913`, which is redundant for ruff (the project ignores that rule globally) but marks the same smell. The state that travels together is the rows collected for one recompute, and the scope of one partition level (the graph, its node ids, the level, the parent community). Two small types replace the accumulator parameters; the recursion keeps the same shape.

Rejected: a module-level dict of three lists (a bag); a generator that yields rows (changes the evaluation order that the write relies on and the row order the tests see); copying the hierarchy constants into the collector (tests patch the module attributes `COMMUNITY_HIERARCHY_SIZE_THRESHOLD` and `COMMUNITY_HIERARCHY_MAX_DEPTH` at run time, so the new code must keep reading the module globals when it decides to recurse).

## 2. Target shape

All private, in `community_detection_service.py`, replacing `_process_partition_group`:

```python
@dataclass(frozen=True, slots=True)
class _PartitionScope:
    """One level of the community hierarchy: the graph being partitioned and where it sits."""
    graph: Any
    node_ids: list[str]
    level: int
    parent_community_id: str | None

@dataclass(slots=True)
class _CommunityRows:
    """The rows one recompute collects, in insertion order, for the transactional write."""
    now: str
    raw_vector: dict[str, Any]
    communities: list[tuple] = field(default_factory=list)
    membership: list[tuple] = field(default_factory=list)
    embeddings: list[tuple] = field(default_factory=list)

    def add_group(self, group_indices: list[int], scope: _PartitionScope) -> None: ...
```

- `add_group(self, group_indices, scope)`: the body of today's `_process_partition_group`. It sorts the group, calls `_compute_community_group(sorted_group, scope.graph, scope.node_ids, self.raw_vector)`, appends the community row `(community_id, representative_id, member_count, scope.level, self.now, scope.parent_community_id)`, appends the embedding row when the centroid is not `None`, and then either recurses (`member_count > COMMUNITY_HIERARCHY_SIZE_THRESHOLD and scope.level < COMMUNITY_HIERARCHY_MAX_DEPTH`, both read as module globals at the time of the call) over `leidenalg.find_partition(child_graph, leidenalg.ModularityVertexPartition)` with a new `_PartitionScope(child_graph, child_node_ids, scope.level + 1, community_id)`, calling `self.add_group(list(child_group), child_scope)` for each child group, or appends the leaf membership rows `(entity_id, community_id, scope.level)` for every member. The lazy imports (`numpy`, `sqlite_vec`, `leidenalg`) stay lazy inside the method, as today.
- `recompute_communities` builds `rows = _CommunityRows(now, raw_vector)` where it creates the three lists today, calls `rows.add_group(list(group), _PartitionScope(graph, node_ids, 0, None))` for each group of the top-level partition, and reads `rows.communities`, `rows.membership` and `rows.embeddings` in `_write` and in the result dict (`communities_created`, `communities_missing_embedding`) exactly where it reads the three lists today.
- `_process_partition_group` and its `# noqa: PLR0913` are deleted. `from dataclasses import dataclass, field` and `Any` are added to the imports if not already present.

Hand count: `add_group` 3 including `self`.

## 3. Decisions

- D1. Behaviour is identical for every input: the order of the appended rows (a parent's community row before its children's, children in partition order), the ids (`uuid4`, one per community), the member ordering (sorted group), the leaf-only membership rows, the embedding rows only for communities with a centroid, the result dict values, and the transactional write (delete both tables, insert the rows, delete and insert the embeddings only when the vector extension is loaded).
- D2. The constants are read from the module at call time inside `add_group` (see section 1).
- D3. No `noqa` is added; the removed `# noqa: PLR0913` is not replaced.
- D4. mypy stays clean: `graph` and the numpy values stay `Any` as they are untyped today; keep the existing `# type: ignore` and `# pyright: ignore` comments only where the annotation they guard still exists.

## 4. Tests (characterization, green before and after)

About 30 scenarios in `tests/test_community_detection_service.py` already cover hierarchy (around lines 740 to 960: oversized split, depth cap, representative and centroid at every level, full recompute clearing every level). Add only what is not exactly pinned, with literal expected values from a run on the unchanged code and the same patching style as the neighbouring tests:

1. The exact tuple shape of a community row, a membership row and an embedding row for a two-level hierarchy (patch the size threshold to 3 and the depth cap to 1, as scenario 32 does): the field order, the level values, the `parent_community_id` of each child equal to its parent's id, and membership rows present only for leaves (relational assertions, as in item 2).
2. The row order for a hierarchy with two top-level groups of which one splits, asserted RELATIONALLY because `leidenalg.find_partition` is called without a seed and is not deterministic: each parent's community row precedes the rows of its children, each top-level group's subtree is contiguous in the insertion order, and membership rows exist only for leaf communities. Use membership sets, never literal ids or list orders, unless the fixture forces the partition (disconnected components); ids are `uuid4` and never literals.
3. `communities_created` and `communities_missing_embedding` in the result when the vector extension is loaded and when it is not. The not-loaded side is already pinned by the scenarios that patch `saltmdb.domain.services.community_detection_service.try_load_vector_extension` (tests 534 to 541 and 572); the developer names the scenario and adds only the missing side, forcing it the same way.

## 5. Out of scope

The other BL-064 slices. The question whether a `noqa: C901/PLR0912/PLR0915` counts as a suppression (this file's `recompute_communities` and four other functions carry one) is a separate decision recorded by the architect with the BL-064 close-out; this slice neither adds nor removes such a comment.

## 6. Acceptance

1. `./verify` exits 0 (judge: exit status and pytest counts).
2. The section 4 tests pass on the unchanged code and after the change (the developer reports both runs; the architect reruns on the final tree).
3. `rg -n "_process_partition_group|communities_to_insert|membership_to_insert|embeddings_to_insert" src tests scripts hooks` returns nothing (the two comment mentions, `config.py` 342 and the `fetch_leaf_community_centroids` docstring, are updated to name `_CommunityRows.add_group`).
4. `git status --short` lists only the three files of section 0.
5. Mutation check on a scratch copy, killer named for each: recurse when `member_count >= threshold`; use `scope.level` instead of `scope.level + 1` for the children; pass the child's own id as `parent_community_id` of itself; append membership rows for non-leaf communities; drop the embedding append; append the child rows before the parent row; copy the threshold into a default at import time. A surviving mutant is reported.
6. Design read (architect and consultant): one responsibility per method, at most 3 parameters, no accumulator parameters.

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| `_process_partition_group` has 10 parameters and two callers (itself and `recompute_communities`) | `community_detection_service.py` 105, 151, 253 | read; scout AST scan |
| No test calls `_process_partition_group`; `_compute_community_group` has one direct test | `rg` over src tests scripts | probed (test line 337) |
| Tests patch the two hierarchy constants on the module | `tests/test_community_detection_service.py` 758, 806, 847, 848, 887, 888, 953 | read |
| The `# noqa: PLR0913` is redundant for ruff | `pyproject.toml` ruff `ignore` contains `PLR0913` | read by the scout |
| `recompute_communities` reads the three lists in `_write` and in the result dict | `community_detection_service.py` 266 to 291 | read; consultant read |
| `leidenalg.find_partition` is called without a seed (non-deterministic) | `community_detection_service.py` ~148, ~232 | consultant read |

## Amendment 1 (2026-10-24): collector name, element types, docstring and two pins

Found at the review of the first implementation (tester: 19-scenario old-versus-new differential identical, 12 of 14 mutants killed; consultant design read: accept with nits).

A1.1 The collector variable in `recompute_communities` is named `community_rows`, not `rows` (section 2 named it `rows`, which collides under mypy with the existing local `rows` that holds the SQL result of the relations query at ~line 203, and the first implementation renamed that SQL local, an edit outside section 0's range). The SQL local keeps its original name `rows`; the diff touches no line of `recompute_communities` above the collector's creation.

A1.2 The three row lists carry precise element types and no `pyright: ignore`: `communities: list[tuple[str, str | None, int, int, str, str | None]]` (the representative is `None` for multi-member groups), `membership: list[tuple[str, str, int]]`, `embeddings: list[tuple[str, bytes]]`. `_PartitionScope` gains the one-line comment `# not hashable: carries a list` (it holds `node_ids` and an igraph graph).

A1.3 The explanatory docstring of the removed `_process_partition_group` moves onto `add_group` (reworded for `scope.*` and `self.*`): one community row per group at `level`; then either recurse into a fresh Leiden pass over its own induced subgraph (if oversized and under the depth cap) or emit leaf membership rows; every community row, leaf or not, gets its own representative and centroid through the unmodified `_compute_community_group` (constraint 25: independently at every level). The `_CommunityRows` docstring says it also drives the Leiden recursion through `add_group`.

A1.4 Two pins the first implementation lacked (tests only): (a) `created_at` of every community row equals the single `now` of the recompute (freeze the clock by patching the module's `datetime`, assert a literal on every row); (b) the group sort is load-bearing: with `leidenalg.find_partition` patched (module attribute, so every recursion level gets it) to return an UNSORTED child group, the child communities and representatives equal those obtained from the same group sorted, so `sorted_group = list(group_indices)` is killed. The real Leiden returns ascending groups in practice (180 of 180 probed runs), so without the fake the mutant is equivalent in practice only by an implementation property of igraph.

A1.5 Test hygiene (consultant): scenario 35 keeps one form of the count/level/parent checks and adds `child_created_at == created_at` and `representative is None or in nodes`; scenario 36 patches `COMMUNITY_HIERARCHY_MAX_DEPTH` to 1 and asserts contiguity of ALL transitive descendants of the split root (one contiguous index range, the flat root outside it), not only direct children; scenario 38 forces the extension state by patching `try_load_vector_extension` as the neighbouring tests do (optionally a mixed case: two disconnected components, one with vectors, `missing == 1` of `created == 2`).
