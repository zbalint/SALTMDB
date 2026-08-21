"""Ranking and supersession post-processing for memory_service.

Pure code-motion extraction (see refactor plan). Has its own top-level
`from datetime import datetime, UTC` -- this is exactly what
test_strict_ranking_defaults.py's `datetime` mock.patch targets, becoming
`saltmdb.domain.services.memory_service.ranking.datetime` after this split
(see the plan's mock.patch string-update stage). No cross-module dependencies
within this package (verified: zero calls into search_primitives.py either
direction).
"""

import math
from datetime import datetime, UTC

from saltmdb.config import SUPERSESSION_CHAIN_MAX_DEPTH
from saltmdb.db.connection import get_connection, close_connection

from ._shared import logger


def _rrf_gap_confident(rrf_score_map: dict[str, float], fts_ids: set, semantic_ids: set) -> bool:
    """True when RRF's top1 candidate is (a) matched by BOTH the FTS and dense-vector channels
    and (b) separated from top2 by RERANK_GAP_SKIP_RATIO or more -- hybrid search already has a
    decisive, corroborated winner and Stage-2 rerank_by_topic has no signal worth adding (see
    SALTMDB memory 870a1d4e, Q8: rerank overrode a dual-channel, ~2x-margin decisive winner with a
    noise-level embedding-cosine call). A tie (Q1-style, ~1.0x, or a top1 matched by only one
    channel) still falls through to rerank -- exactly the ambiguous case rerank helps with.
    Requiring dual-channel support, not ratio alone, avoids trusting a numeric gap that isn't
    actually backed by real retrieval agreement (see RERANK_GAP_SKIP_RATIO's config.py comment for
    the calibration data behind this).
    """
    ids = list(rrf_score_map.keys())
    scores = list(rrf_score_map.values())
    if len(scores) < 2 or scores[1] <= 0:
        return False
    top1_id = ids[0]
    if top1_id not in fts_ids or top1_id not in semantic_ids:
        return False
    from saltmdb.config import RERANK_GAP_SKIP_RATIO

    return (scores[0] / scores[1]) >= RERANK_GAP_SKIP_RATIO


def _apply_type_bias(ordered_ids: list, conn) -> list:
    """Part 2 (SALTMDB memory 870a1d4e, prefer_durable_types): stable-partitions `event`-typed
    candidates to the back of ordered_ids, preserving relative order within each group. `event`
    memories are working/session notes prone to staleness by design (see SALTMDB memory 870a1d4e's
    Q12 case) -- the other four memory_type values (fact/decision/procedure/preference) are
    treated as durable and kept in front. No-op on an empty pool.
    """
    if not ordered_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"SELECT id, memory_type FROM entities WHERE id IN ({placeholders})", ordered_ids
    ).fetchall()
    event_ids = {row[0] for row in rows if row[1] == "event"}
    return [eid for eid in ordered_ids if eid not in event_ids] + [
        eid for eid in ordered_ids if eid in event_ids
    ]


def _compute_superseded_ids(ordered_ids: list, conn) -> set:
    """Shared query: ids within ordered_ids that are the TARGET of a currently-valid outgoing
    `supersedes` edge (`A supersedes B` -> B, the target, is the old/superseded one -- matches this
    codebase's `consolidated_from` precedent of source=new/target=old). Factored out of
    `_apply_supersession_demotion` so mode="history" (Part C) can reuse the exact same
    single-hop "is this superseded right now" check to TAG candidates without demoting or hiding
    them, instead of duplicating the SQL. No-op (empty set) on an empty pool.
    """
    if not ordered_ids:
        return set()
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT target_id FROM relations
        WHERE target_id IN ({placeholders}) AND predicate = 'supersedes'
          AND (valid_to IS NULL OR datetime(valid_to) > datetime('now'))
        """,
        ordered_ids,
    ).fetchall()
    return {row[0] for row in rows}


def _compute_bitemporal_target_ids(ordered_ids: list, conn, predicate: str, now: str) -> set:
    """Shared core: ids within ordered_ids that are the TARGET of a currently-valid outgoing
    `predicate` edge, "currently valid" meaning the full four-column bitemporal predicate
    (`valid_from`/`valid_to`/`valid_at`/`invalid_at`) holds at the single caller-supplied `now`
    instant -- not each column checked against its own independently-sampled clock read. Callers
    that need internal consistency across multiple predicate checks (e.g.
    `_apply_strict_ranking_defaults` checking both `supersedes` and `corrects`) MUST capture `now`
    once and pass the same value into every call, or a candidate could be classified differently by
    two checks that should agree (SALTMDB roadmap ba2cf66f P1#6 plan, Codex round-1 finding). No-op
    (empty set) on an empty pool.
    """
    if not ordered_ids:
        return set()
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT target_id FROM relations
        WHERE target_id IN ({placeholders}) AND predicate = ?
          AND (valid_from IS NULL OR datetime(valid_from) <= datetime(?))
          AND (valid_to IS NULL OR datetime(valid_to) > datetime(?))
          AND (valid_at IS NULL OR datetime(valid_at) <= datetime(?))
          AND (invalid_at IS NULL OR datetime(invalid_at) > datetime(?))
        """,
        ordered_ids + [predicate, now, now, now, now],
    ).fetchall()
    return {row[0] for row in rows}


def _compute_superseded_ids_bitemporal(ordered_ids: list, conn) -> set:
    """mode="history"'s own single-hop "is this superseded right now" check (Part C) -- NOT the
    same query as `_compute_superseded_ids` above (Codex review P1 finding, correctly caught): that
    function only checks `valid_to`, a pre-existing precedent from `_apply_supersession_demotion`
    which this plan explicitly leaves unchanged ("stays single-hop, sink-to-bottom, and unchanged"
    -- see that function's own docstring). But `history` mode's own docs promise `is_superseded`
    reflects a "currently-valid" edge in the same full bitemporal sense Part A's resolver uses, so
    it needs the same four-column predicate (`valid_from`/`valid_to`/`valid_at`/`invalid_at`), not
    demote_superseded's narrower single-column one -- reusing `_apply_supersession_demotion`'s
    check here would silently tag an edge whose `valid_from` is still in the future, or whose
    `invalid_at` has already passed, as "currently superseding" when it isn't. No-op on an empty
    pool.

    Thin wrapper over `_compute_bitemporal_target_ids` (SALTMDB roadmap ba2cf66f P1#6 plan) --
    own `now` sample per call, matching this function's pre-existing single-call-site behavior
    under mode="history" (which never needs cross-predicate consistency, unlike
    `_apply_strict_ranking_defaults` below).
    """
    return _compute_bitemporal_target_ids(
        ordered_ids, conn, "supersedes", datetime.now(UTC).isoformat()
    )


def _apply_supersession_demotion(ordered_ids: list, conn) -> list:
    """Part 2 (SALTMDB memory 870a1d4e, demote_superseded): stable-partitions candidates that are
    the TARGET of a currently-valid outgoing `supersedes` edge to the back of ordered_ids,
    preserving relative order within each group. `A supersedes B` means A (source) is the
    new/authoritative memory and B (target) is the old one it replaces -- matching this codebase's
    `consolidated_from` precedent (source = new summary, target = old raw parent) -- so it is the
    target side that gets demoted here, not the source (corrects a direction bug in 870a1d4e's own
    original wording, confirmed during implementation review). Uses this file's own existing
    "currently valid" literal idiom (see the related_map query above) rather than
    relation_service's separate point-in-time parameter style. No-op on an empty pool.

    This is single-hop, sink-to-bottom, independently-togglable demotion -- structurally separate from the
    multi-hop chain-resolution *substitution* `_resolve_supersession_chains` performs for
    mode="strict" below (Part A of plans/scalable-strolling-stallman.md); this flag/function is
    unchanged by that work.
    """
    if not ordered_ids:
        return []
    superseded_ids = _compute_superseded_ids(ordered_ids, conn)
    return [eid for eid in ordered_ids if eid not in superseded_ids] + [
        eid for eid in ordered_ids if eid in superseded_ids
    ]


def _collapse_supersedes_families(  # noqa: C901, PLR0912, PLR0915
    ordered_ids: list[str], conn
) -> list[str]:
    """Collapse eligible broad-mode ``supersedes`` chains without injecting rows.

    A family is eligible only when its active, full-bitemporal supersedes component is an
    acyclic, nonforking chain, all of its members are live, and every member (including the
    newest head) is already present in ``ordered_ids`` after the caller's filters.  The scan is
    intentionally left-to-right: the first family member emits the head's own id at that exact
    position, while subsequent members are skipped.  Because the head must already be in the
    pool, this operation never bypasses a filter or fabricates a row/score.

    This helper is deliberately separate from strict/history behavior.  Strict resolves and
    substitutes each matched predecessor through its existing relevance gate; history preserves
    every matched row and tags it.  Neither mode calls this family-collapse operation.
    """
    if not ordered_ids:
        return []
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        """
        SELECT r.source_id, r.target_id
        FROM relations r
        WHERE r.predicate = 'supersedes'
          AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
          AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
          AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
          AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
        """,
        (now, now, now, now),
    ).fetchall()
    if not rows:
        return list(ordered_ids)

    source_to_targets: dict[str, set[str]] = {}
    target_to_sources: dict[str, set[str]] = {}
    undirected: dict[str, set[str]] = {}
    for source_id, target_id in rows:
        source_to_targets.setdefault(source_id, set()).add(target_id)
        target_to_sources.setdefault(target_id, set()).add(source_id)
        undirected.setdefault(source_id, set()).add(target_id)
        undirected.setdefault(target_id, set()).add(source_id)

    pool = set(ordered_ids)
    touched = set()
    components: dict[str, tuple[set[str], str] | None] = {}
    # Only inspect components that touch this result pool.  The relation scan is global so a
    # branch/future/archive node outside the pool still makes the touched family ineligible.
    for seed in pool:
        if seed not in undirected or seed in touched:
            continue
        component: set[str] = set()
        stack = [seed]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(undirected.get(node, ()))
        touched.update(component)

        eligible = component <= pool
        info_rows = []
        if eligible:
            placeholders = ",".join("?" for _ in component)
            info_rows = conn.execute(
                f"SELECT id, status FROM entities WHERE id IN ({placeholders})", list(component)
            ).fetchall()
            info = {row[0]: row[1] for row in info_rows}
            eligible = len(info) == len(component) and all(
                status != "archived" for status in info.values()
            )
        # A nonforking chain has at most one older target per newer source and at most one newer
        # source per older target.  The live head is the newest node with no newer source pointing
        # at it (no incoming edge); the scan emits this head in the first member's position.
        if eligible:
            eligible = all(
                len(source_to_targets.get(node, ())) <= 1
                and len(target_to_sources.get(node, ())) <= 1
                for node in component
            )
            heads = [node for node in component if not target_to_sources.get(node)]
            eligible = eligible and len(heads) == 1
            head = heads[0] if eligible else ""
            if eligible:
                walked: set[str] = set()
                node = head
                while node not in walked:
                    walked.add(node)
                    successors = source_to_targets.get(node, set())
                    if not successors:
                        break
                    node = next(iter(successors))
                eligible = walked == component and node not in source_to_targets
        else:
            head = ""
        for member in component:
            components[member] = (component, head) if eligible else None

    emitted: list[str] = []
    skipped: set[str] = set()
    for entity_id in ordered_ids:
        if entity_id in skipped:
            continue
        family = components.get(entity_id)
        if family is None:
            emitted.append(entity_id)
            continue
        members, head = family
        emitted.append(head)
        skipped.update(members)
    return emitted


def _build_cross_encoder_candidate_texts(  # noqa: C901, PLR0912, PLR0915
    query_text: str,
    candidate_ids: list[str],
    conn,
    db_path: str,
) -> dict[str, str]:
    """Build CE inputs from title + the best fresh query-matching chunk.

    ``entity_chunk_embeddings`` is read only for the capped candidate prefix.  A chunk is
    eligible only when its content hash matches the live entity hash and the entity is not
    archived.  If no fresh chunk exists, the authoritative entity title plus leading content is
    returned.  The final character cap remains in ``reranker_service.score_pairs`` so direct
    callers and benchmark probes share one truncation rule.
    """
    if not candidate_ids:
        return {}
    placeholders = ",".join("?" for _ in candidate_ids)
    entity_rows = conn.execute(
        f"""
        SELECT id, title, full_content, content_hash
        FROM entities
        WHERE id IN ({placeholders}) AND status != 'archived'
        """,
        list(candidate_ids),
    ).fetchall()
    base = {row[0]: (row[1] or "", row[2] or "", row[3]) for row in entity_rows}
    if not base:
        return {}

    best_chunks: dict[str, tuple[float, int | None, int | None, str]] = {}
    chunk_conn = None
    try:
        import sqlite_vec
        from saltmdb.domain.services import embedding_service

        query_vector = embedding_service.embed_query_text(query_text)
        chunk_conn = get_connection(db_path)
        chunk_conn.enable_load_extension(True)
        sqlite_vec.load(chunk_conn)
        chunk_conn.enable_load_extension(False)
        fresh_ids = list(base)
        fresh_placeholders = ",".join("?" for _ in fresh_ids)
        rows = chunk_conn.execute(
            f"""
            SELECT c.entity_id, c.chunk_index, c.char_start, c.char_end,
                   e.full_content, vec_distance_cosine(c.embedding, ?) AS distance
            FROM entity_chunk_embeddings c
            JOIN entities e ON e.id = c.entity_id
            WHERE c.entity_id IN ({fresh_placeholders})
              AND e.status != 'archived'
              AND c.content_hash IS e.content_hash
            """,
            [sqlite_vec.serialize_float32(query_vector)] + fresh_ids,
        ).fetchall()
        for entity_id, chunk_index, char_start, char_end, content, distance in rows:
            try:
                distance_value = float(distance)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(distance_value):
                continue
            prior = best_chunks.get(entity_id)
            if prior is None or distance_value < prior[0]:
                best_chunks[entity_id] = (
                    distance_value,
                    char_start,
                    char_end,
                    str(content or ""),
                )
    except Exception as exc:
        # CE text construction is an optional enhancement.  A missing sqlite_vec/chunk table
        # must fall back to authoritative leading content, never suppress an otherwise valid CE
        # rerank call.
        logger.debug("Fresh chunk lookup for cross-encoder inputs failed: %s", exc)
    finally:
        if chunk_conn:
            close_connection(chunk_conn)

    result: dict[str, str] = {}
    for entity_id in candidate_ids:
        if entity_id not in base:
            continue
        title, content, _content_hash = base[entity_id]
        best = best_chunks.get(entity_id)
        if best is not None:
            _distance, char_start, char_end, chunk_content = best
            if char_start is not None and char_end is not None:
                try:
                    chunk_text = content[int(char_start) : int(char_end)]
                except (TypeError, ValueError):
                    chunk_text = chunk_content
            else:
                chunk_text = chunk_content
            evidence = chunk_text or content[:2000]
        else:
            evidence = content[:2000]
        result[entity_id] = f"{title}\n\n{evidence}"
    return result


def _apply_strict_ranking_defaults(ordered_ids: list, conn) -> list:
    """mode="strict"-only forced ranking defaults (SALTMDB roadmap ba2cf66f P1#6, design 1fddc04a):
    durable-type preference + a residual-supersession/correction safety-net demotion, applied
    unconditionally regardless of the caller's own prefer_durable_types/demote_superseded flags
    (which keep their existing, independently-togglable meaning for broad/history -- untouched by this
    function). Order matches _apply_type_bias-then-demotion's existing precedent (Part 2, SALTMDB
    memory 870a1d4e): type bias first, so an explicitly stale/wrong item always sinks below a
    merely-event-typed one, not the reverse.

    Covers two cases `demote_superseded`/`_resolve_supersession_chains` don't, by design:
    - A candidate Part A's chain resolver abstained on (cycle, depth-cap breach, or archived
      intermediate node) stays in the pool under its original id, still bitemporally superseded --
      substitution deliberately declined to touch it, so without this safety net it would rank as
      if authoritative.
    - A candidate that is the target of a currently-valid `corrects` edge -- a predicate Part A's
      resolver has no concept of (it only walks `supersedes` chains), and `demote_superseded`
      (kept intentionally unchanged) never checked either.

    One `now` captured here and passed into both bitemporal lookups so a validity-boundary-
    straddling edge can't be classified differently by the two predicate checks (Codex plan-review
    round-1 finding, `plans/amber-sifting-falcon.md`). Demoted ids are unioned (not two sequential
    partitions) so an id caught by both checks sinks once, not double-processed -- both signal "this
    specific memory is known wrong/outdated," treated as one demotion tier. Demotion changes
    position only, never presence -- a demoted candidate already independently cleared
    accept_or_abstain's gate on its own evidence merits before this function ever sees it. No-op on
    an empty pool.
    """
    if not ordered_ids:
        return []
    ordered_ids = _apply_type_bias(ordered_ids, conn)
    now = datetime.now(UTC).isoformat()
    demoted = _compute_bitemporal_target_ids(
        ordered_ids, conn, "supersedes", now
    ) | _compute_bitemporal_target_ids(ordered_ids, conn, "corrects", now)
    if not demoted:
        return ordered_ids
    return [eid for eid in ordered_ids if eid not in demoted] + [
        eid for eid in ordered_ids if eid in demoted
    ]


def _resolve_supersession_chains(  # noqa: C901
    conn,
    candidate_ids: list[str],
    where_clauses: list[str],
    params: list,
    max_depth: int = SUPERSESSION_CHAIN_MAX_DEPTH,
) -> dict[str, str]:
    """Part A (multi-hop supersession-chain resolution, plans/scalable-strolling-stallman.md, for
    search_memory's mode="strict"): for each id in candidate_ids, walk the currently-valid
    `supersedes` chain forward (`A supersedes B` => A is the newer/authoritative node, B is the old
    one being replaced) to its live, fully-revalidated terminal head, and return
    {candidate_id: resolved_head_id} for candidates that successfully resolved to a DIFFERENT id.
    A candidate absent from the returned dict either has no live supersessor at all (nothing to
    substitute) or hit an abstain condition below -- both mean "use the original id, unsubstituted"
    to the caller, by design: a depth-capped or cycle-cut path is not safely treated as a terminal
    head, so it must never be silently substituted.

    Batched over the whole candidate pool in a single recursive-CTE round trip (same
    IN (...)-batched idiom as `_apply_type_bias`/`_compute_superseded_ids` above -- not a
    per-candidate loop), bounded to edges actually reachable from this pool within max_depth+1
    hops. The CTE enumerates every reachable (root, hop) edge -- it deliberately does NOT attempt
    to prune at a fork mid-recursion (a SQLite recursive CTE can't safely express "greedily keep
    only the tie-break winner, discard the rest" without a fragile correlated-subquery rewrite);
    instead all candidate edges are returned, and the correctness-critical tie-break/cycle/
    depth-cap/liveness decisions are made by one deterministic Python walk per candidate below,
    over the small in-memory edge set the query returns. This keeps the "one DB round trip,
    batched over the whole pool" property the plan calls for while keeping the actual fork/abstain
    logic auditable and unit-testable in plain Python instead of opaque SQL.

    The SQL recursion bound is `depth <= max_depth` alone -- NOT a path-based cycle guard like
    analyze_lineage/analyze_dependencies' own `NOT LIKE '%'||id||'%'` precedent (a real bug caught
    during test-writing: a path-based SQL guard silently DROPS the row that would reveal a cycle,
    which then looks indistinguishable from "genuinely terminal" to the code below it -- an actual
    two-node A<->B cycle was mis-resolved to a live successor instead of abstaining, before this
    was caught). The depth cap alone still guarantees SQL termination even on a real cycle
    (bounded, repeated re-visits up to max_depth+1 rows), and cycle detection is instead done
    exactly once, correctly, in the Python walk's own `visited` set below -- one source of truth
    for "is this a cycle," not two that could disagree.

    Design decisions pinned down explicitly (per the plan's own callout not to leave these
    implicit):
    - "Currently valid" for the `supersedes` EDGE is evaluated across all four bitemporal columns
      (valid_from/valid_to/valid_at/invalid_at) at one captured `now` for the whole call -- not a
      partial check like analyze_lineage's existing CTE (relation_service.py), which omits
      valid_at.
    - "Live" for a traversed NODE means `entities.status != 'archived'` only -- entities.valid_from/
      valid_to are NOT independently re-checked, because in this codebase they only ever move in
      lockstep with status (store_memory's temporal-upsert and archive_memory both only ever set
      valid_to alongside status='archived'; a live entity's own valid_to is always NULL), so a
      separate check would be redundant, not additive. This matches search_memory's own existing
      liveness precedent (`e.status != 'archived'` in its where_clauses).
    - Tie-break at a fork (two-plus currently-valid edges targeting the same node -- the relations
      table's partial unique index only guarantees uniqueness per (source, target) pair, not per
      target) is the successor's updated_at, then created_at, then id, all descending -- applied
      greedily at EVERY hop of the walk, not just the final target, so a fork mid-chain resolves
      the same deterministic way as a fork at the seed.
    - Cycle, depth-cap breach (a chain that needs more than max_depth hops to terminate), or an
      inaccessible/archived intermediate node anywhere in the chain: abstain on that candidate
      entirely (see module docstring above for what "abstain" means to the caller).
    - The resolved head is re-checked against the ORIGINAL query's own where_clauses/params
      (owner_id/scope, context_id, is_core, memory_type_filter, tags_filter) before being
      returned -- analyze_lineage/analyze_dependencies are unfiltered admin tools, search_memory is
      not, and a resolved head is not necessarily visible to this particular caller.
    """
    if not candidate_ids:
        return {}

    now = datetime.now(UTC).isoformat()
    validity_sql = (
        "(r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?)) AND "
        "(r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?)) AND "
        "(r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?)) AND "
        "(r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))"
    )
    placeholders = ",".join("?" for _ in candidate_ids)
    query = f"""
        WITH RECURSIVE chain(root_id, current_id, next_id, depth) AS (
            SELECT r.target_id, r.target_id, r.source_id, 1
            FROM relations r
            WHERE r.target_id IN ({placeholders}) AND r.predicate = 'supersedes'
              AND {validity_sql}

            UNION ALL

            SELECT c.root_id, c.next_id, r.source_id, c.depth + 1
            FROM relations r
            JOIN chain c ON r.target_id = c.next_id
            WHERE r.predicate = 'supersedes' AND c.depth <= ?
              AND {validity_sql}
        )
        SELECT DISTINCT root_id, current_id, next_id FROM chain
    """
    exec_params = list(candidate_ids) + [now, now, now, now, max_depth, now, now, now, now]
    edge_rows = conn.execute(query, exec_params).fetchall()
    if not edge_rows:
        return {}

    adjacency: dict[str, set] = {}
    touched_ids: set = set(candidate_ids)
    roots_with_edges: set = set()
    for root_id, current_id, next_id in edge_rows:
        adjacency.setdefault(current_id, set()).add(next_id)
        touched_ids.add(current_id)
        touched_ids.add(next_id)
        roots_with_edges.add(root_id)

    entity_placeholders = ",".join("?" for _ in touched_ids)
    entity_rows = conn.execute(
        f"SELECT id, status, updated_at, created_at FROM entities WHERE id IN ({entity_placeholders})",
        list(touched_ids),
    ).fetchall()
    entity_info = {
        row[0]: {"status": row[1], "updated_at": row[2], "created_at": row[3]}
        for row in entity_rows
    }

    def _tie_break(next_ids: set) -> str:
        def key(nid: str):
            info = entity_info.get(nid, {})
            return (info.get("updated_at") or "", info.get("created_at") or "", nid)

        return max(next_ids, key=key)

    _ABSTAIN = object()

    def _walk(root_id: str):
        node = root_id
        visited = {root_id}
        depth = 0
        while True:
            next_ids = adjacency.get(node)
            if not next_ids:
                break  # terminal: node has no further live supersessor
            if depth + 1 > max_depth:
                return _ABSTAIN  # chain continues beyond the allowed cap
            chosen = _tie_break(next_ids)
            info = entity_info.get(chosen)
            if not info or info["status"] == "archived":
                return _ABSTAIN  # inaccessible/archived intermediate (or terminal) node
            if chosen in visited:
                return _ABSTAIN  # cycle
            visited.add(chosen)
            node = chosen
            depth += 1
        return node if node != root_id else None

    resolved: dict[str, str] = {}
    for root_id in roots_with_edges:
        outcome = _walk(root_id)
        if outcome is not None and outcome is not _ABSTAIN:
            resolved[root_id] = outcome

    if not resolved:
        return {}

    # Filter-reapplication: the resolved head must independently satisfy the ORIGINAL query's own
    # where_clauses/params -- a resolved head is not necessarily visible to this particular caller
    # (owner/scope/context/is_core/memory_type/tags filters all still apply).
    heads = set(resolved.values())
    head_placeholders = ",".join("?" for _ in heads)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    passing_rows = conn.execute(
        f"SELECT e.id FROM entities e WHERE e.id IN ({head_placeholders}) AND {where_sql}",
        list(heads) + list(params),
    ).fetchall()
    passing_heads = {row[0] for row in passing_rows}

    return {cid: head for cid, head in resolved.items() if head in passing_heads}


def _substitute_resolved_heads(
    rrf_score_map: dict[str, float], resolved_map: dict[str, str]
) -> dict[str, float]:
    """Part A dedup-merge rule: substitutes every candidate in rrf_score_map that appears in
    resolved_map (from `_resolve_supersession_chains`) with its resolved head, merging colliding
    heads to the MAX score (never sum) -- avoids RRF-score inflation when multiple, otherwise
    unrelated pool entries collapse onto the same live head. Returns a new dict re-sorted by score
    descending (merging can change relative order versus the input). No-op (returns a re-sorted
    copy) when resolved_map is empty.
    """
    substituted: dict[str, float] = {}
    for cid, score in rrf_score_map.items():
        head = resolved_map.get(cid, cid)
        if head in substituted:
            substituted[head] = max(substituted[head], score)
        else:
            substituted[head] = score
    return dict(sorted(substituted.items(), key=lambda kv: -kv[1]))


def _build_candidate_evidence(
    pool_ids: list[str],
    rrf_score_map: dict[str, float],
    fts_rows: list,
    semantic_rows: list[tuple[str, float]],
    topic_scores_map: dict[str, dict],
    resolved_from: dict[str, list[str]],
    predecessor_grounded_map: dict[str, bool] | None = None,
    cross_encoder_scores_map: dict[str, float] | None = None,
    *,
    used_or_fallback: bool = False,
) -> dict[str, dict]:
    """Part B: builds a per-candidate evidence record for every id in pool_ids, consumed by
    `accept_or_abstain` below. Two independent evidence axes are tracked:

    - DIRECT vs INDIRECT provenance (Codex correction to the first plan draft, which conflated
      them):
      - DIRECT: the candidate itself appeared in the FTS and/or semantic retrieval pool -- it has
        its own native rank/score/lexical-match signal (`in_fts`/`in_semantic`/`fts_rank`/
        `semantic_distance`/etc below).
      - INDIRECT: the candidate is a resolved supersession head (present in `resolved_from`) that
        was NOT itself in the original, pre-resolution retrieval pool. It has no native FTS/semantic
        rank of its own -- copying the superseded predecessor's evidence onto it would be unsound
        (the predecessor matched the query; the successor's own relation to the query is
        unverified). This function deliberately leaves an indirect candidate's own direct-evidence
        fields as their natural empty/None/False values; `predecessor_grounded_map` (built by the
        caller from the PRE-resolution pool's own evidence, see search_memory) is threaded through
        instead, so `accept_or_abstain` can require the predecessor's own match to have been strong,
        per the plan's explicit "requiring the predecessor's match to have been strong" option.
        A resolved head CAN also independently appear directly in the pool -- both classes can
        coexist; `provenance` is "direct" whenever there's any native signal at all, "indirect" only
        when there is none.
    - AND-match vs OR-fallback-only, for DIRECT FTS matches (H1 fix): `used_or_fallback` is the
      single pool-level bool `_run_fts_search` returns when `return_fallback_flag=True` -- True iff
      the AND-joined MATCH found nothing and the OR-joined retry is what produced `fts_rows`. When
      True, every id in `fts_rows` is an OR-fallback-only match (`in_fts_or_only`); when False,
      every id in `fts_rows` is a genuine AND match (`in_fts_and`) -- this is a property of how the
      whole query was executed, not a per-row distinction. `in_fts` stays `in_fts_and or
      in_fts_or_only` for any caller that doesn't care about the split (broad/history mode ranking
      keeps using it for recall, unchanged). `dual_channel` is keyed specifically on `in_fts_and`
      (NOT the broader `in_fts`) -- an OR-fallback-only candidate that also happens to land in the
      semantic pool must still go through `accept_or_abstain`'s `in_fts_or_only` topic-grounding
      check, not bypass it via the dual-channel shortcut (this was a real bypass caught during
      review; do not "simplify" `dual_channel` back to `in_fts and in_semantic`).

    `topic_score`/`semantic_verdict` stay optional (None when absent) -- populated only when
    rerank_by_topic actually ran for this call (cost note, Part B): this function must never force
    that expensive Stage-2 pass as a side effect of being called.

    `cross_encoder_score` (roadmap `ba2cf66f` P1#7) is the same optional-field shape: `None`
    unless `use_cross_encoder` actually scored this candidate. Evidence only, this release --
    `accept_or_abstain` does NOT read this field yet (see search_memory's own docstring for why).
    """
    fts_rank = {row[0]: i for i, row in enumerate(fts_rows)}
    fts_bm25 = {row[0]: row[5] for row in fts_rows}
    semantic_rank = {eid: i for i, (eid, _dist) in enumerate(semantic_rows)}
    semantic_distance = {eid: dist for eid, dist in semantic_rows}
    predecessor_grounded_map = predecessor_grounded_map or {}
    cross_encoder_scores_map = cross_encoder_scores_map or {}

    evidence: dict[str, dict] = {}
    for eid in pool_ids:
        in_fts = eid in fts_rank
        in_fts_and = in_fts and not used_or_fallback
        in_fts_or_only = in_fts and used_or_fallback
        in_semantic = eid in semantic_rank
        has_direct_signal = in_fts or in_semantic
        is_resolved_head = eid in resolved_from
        provenance = "direct" if has_direct_signal or not is_resolved_head else "indirect"
        topic = topic_scores_map.get(eid)
        evidence[eid] = {
            "entity_id": eid,
            "provenance": provenance,
            "rrf_score": rrf_score_map.get(eid),
            "in_fts": in_fts,
            "in_fts_and": in_fts_and,
            "in_fts_or_only": in_fts_or_only,
            "fts_rank": fts_rank.get(eid),
            "fts_bm25": fts_bm25.get(eid),
            "in_semantic": in_semantic,
            "semantic_rank": semantic_rank.get(eid),
            "semantic_distance": semantic_distance.get(eid),
            "dual_channel": in_fts_and and in_semantic,
            "topic_score": topic["topic_score"] if topic else None,
            "semantic_verdict": topic["semantic_verdict"] if topic else None,
            "is_resolved_head": is_resolved_head,
            "predecessor_grounded": predecessor_grounded_map.get(eid, False),
            "cross_encoder_score": cross_encoder_scores_map.get(eid),
        }
    return evidence


def accept_or_abstain(evidence: dict, policy: dict | None = None) -> tuple[bool, str]:  # noqa: PLR0911
    """Part B: pure function deciding whether ONE candidate's evidence record clears the
    relevance-abstention gate. Called per-candidate (not just against top-1) by search_memory's
    mode="strict" path; an empty resulting pool after filtering every candidate is the `[]` case
    (SALTMDB memory `c27792a1`). `policy` is accepted for future extension/testability but unused
    today -- this function consumes only the precomputed categorical `semantic_verdict` already
    attached to `evidence` (see `_build_candidate_evidence`); it reads no config.py threshold of
    its own (unlike `_rrf_gap_confident`'s own direct RERANK_GAP_SKIP_RATIO import) -- the
    SAME_SPECIFIC_TOPIC/BROADLY_RELATED_THEMES/DIFFERENT_TOPICS classification and its underlying
    RERANK_SAME_TOPIC_THRESHOLD/RERANK_BROAD_THEME_THRESHOLD live in rerank_candidates_by_topic.

    Acceptance requires a positive grounding signal -- a real, non-phantom match this codebase can
    already produce:

    - DIRECT, dual-channel (in_fts_and AND in_semantic): always accepted -- the strongest, already-
      established signal shape (`_rrf_gap_confident`'s own precondition for even considering a
      confident top-1). Deliberately keyed on `in_fts_and`, not the broader `in_fts` -- an
      OR-fallback-only match that also happens to land in the semantic pool must NOT take this
      shortcut; it goes through the `in_fts_or_only` rule below instead (H1 fix; this is the exact
      bypass an earlier revision of this gate had).
    - DIRECT, true FTS AND-match (in_fts_and, not in_semantic): accepted -- a genuine AND-joined
      `entities_fts MATCH` is a real term match, not a nearest-neighbor phantom, and its
      correctness doesn't degrade as the corpus grows.
    - DIRECT, FTS OR-fallback-only (in_fts_or_only -- present only because the AND-joined query
      found nothing and `_run_fts_search` silently retried with an OR-joined query), REGARDLESS of
      whether it also happens to be `in_semantic`: accepted ONLY if `semantic_verdict` is exactly
      "SAME_SPECIFIC_TOPIC", the same bar as the semantic-only rule below. An OR-fallback match is
      an incidental single-term hit, not a corroborated match on its own -- unconditionally
      accepting it (as an earlier revision of this gate did, via the old broad `in_fts` check) is
      exactly the false-accept mechanism SALTMDB memory `6ee96334` traced 10/10 replayed
      negative-control queries to.
    - DIRECT, semantic-only (in_semantic, neither in_fts_and nor in_fts_or_only): accepted ONLY if
      `semantic_verdict` is exactly "SAME_SPECIFIC_TOPIC" (reusing RERANK_SAME_TOPIC_THRESHOLD
      as-is, not a new constant -- see the "why not a raw distance cutoff" note below).
      "BROADLY_RELATED_THEMES" or no topic_score at all is NOT sufficient on its own.
    - INDIRECT (resolved supersession head absent from the original pool): has no native signal of
      its own to trust. Accepted only if `predecessor_grounded` is True -- the original,
      pre-resolution candidate that resolved to this head independently cleared the DUAL_CHANNEL,
      true-AND FTS_MATCH, or (since the H1 fix) the OR-fallback-plus-SAME_SPECIFIC_TOPIC rule above
      (NOT the semantic-only rule; see search_memory's predecessor-evidence construction). An
      OR-fallback-only predecessor's `predecessor_grounded` value can therefore itself depend on an
      on-demand topic-verdict lookup performed over the pre-resolution pool before this map is
      built -- not only on `dual_channel`/`in_fts_and` signals, as an earlier revision of this
      docstring implied. This is deliberately the ONLY condition (an earlier version of this function also required
      the head's own semantic_verdict not be "DIFFERENT_TOPICS" -- reverted: the on-demand
      topic-grounding lookup that powers the DIRECT semantic-only rule above assigns EVERY
      FTS-less candidate some verdict, including a default "DIFFERENT_TOPICS" for a resolved head
      with no embedding data at all to score -- indistinguishable in the data from "genuinely
      off-topic," so it was vetoing legitimately-grounded resolved heads that simply had no
      chunk/entity embedding yet. Plan section B explicitly frames "predecessor was strong" as a
      sufficient condition on its own, not one that must also be combined with the head's own
      weak/absent signal).
    - No evidence at all (neither direct nor a grounded indirect path): abstain.

    Why not a raw semantic_distance cutoff (what an earlier version of this function did): empirically
    disproven during implementation verification. Measured live against the 21k-entity diverse
    test corpus (scratch/diverse_corpus_full.db), a genuinely unrelated/nonsense query's nearest
    entity-embedding neighbor routinely lands at cosine distance 0.22-0.34 -- fully overlapping the
    0.2152-0.3677 range measured for HAND-VERIFIED GENUINE semantic paraphrase matches in a small
    control corpus. A single whole-document embedding vector's absolute distance to an unrelated
    query shrinks as the candidate pool grows (more documents means a better chance some unrelated
    one is coincidentally "close"), so a fixed distance floor that looks well-calibrated on a small
    corpus silently stops discriminating at real scale -- it is not a fixable-by-retuning problem,
    it is the wrong signal shape. A rank/margin-based check over the same raw vectors was tried
    next and also failed: genuine and nonsense queries showed statistically indistinguishable
    rank-1-vs-rank-2 distance gaps against the full corpus (both types of query land in a densely
    clustered neighborhood of similar-distance candidates). Chunk-level topic_score
    (rerank_candidates_by_topic) was tried third and is what this function actually uses --
    but even that alone is NOT precise enough to cleanly separate the two classes at the
    "BROADLY_RELATED_THEMES" tier (both genuine and nonsense queries commonly land there); only
    the stricter, already-calibrated "SAME_SPECIFIC_TOPIC" tier reliably excludes the nonsense
    class in live testing, at the accepted cost of also abstaining on some genuine but only
    loosely/broadly-paraphrased semantic-only matches -- matching this codebase's explicit,
    stated risk asymmetry (0% false-accept is the hard target; a nonzero false-reject rate on
    weak, uncorroborated matches is accepted, not hidden) and directly implementing design memory
    `b9b75764`'s "treat weak vector-only proximity as insufficient."
    """
    if evidence.get("provenance") == "indirect":
        if not evidence.get("predecessor_grounded"):
            return False, "indirect_ungrounded_predecessor"
        return True, "indirect_grounded_predecessor"

    if evidence.get("dual_channel"):
        return True, "dual_channel"
    if evidence.get("in_fts_and"):
        return True, "fts_match"
    if evidence.get("in_fts_or_only"):
        if evidence.get("semantic_verdict") == "SAME_SPECIFIC_TOPIC":
            return True, "fts_or_fallback_same_specific_topic"
        return False, "fts_or_fallback_insufficient_topic_grounding"
    if evidence.get("in_semantic"):
        if evidence.get("semantic_verdict") == "SAME_SPECIFIC_TOPIC":
            return True, "semantic_only_same_specific_topic"
        return False, "semantic_only_insufficient_topic_grounding"
    return False, "no_evidence"
