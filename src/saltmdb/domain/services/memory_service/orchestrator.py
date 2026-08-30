"""search_memory orchestrator for memory_service.

Pure code-motion extraction (see refactor plan) -- search_memory's ~940-line body is
moved verbatim, including its nested `_compute_pool` closure. Its internal calls into
validation/search_primitives/ranking/tags are rewritten to qualified module-attribute
access (`search_primitives.semantic_search(...)` rather than a bare `semantic_search(...)`
from a `from .search_primitives import semantic_search` binding) -- this is deliberate,
not an oversight: it's what keeps `unittest.mock.patch("...memory_service.search_primitives.X")`
-style patches able to intercept these calls after the split (patch-where-looked-up must
equal patch-where-defined). This function's internal decomposition (breaking its ~940
lines into smaller pieces) is explicitly NOT attempted in this pass -- tracked as
deferred follow-up work, see the refactor plan's section 3.
"""

import json
import re
from typing import Any, Literal

from saltmdb.config import get_db_path, STRICT_OVERFETCH_CANDIDATE_CAP
from saltmdb.db.connection import get_connection, close_connection
from saltmdb.utils.text import sanitize_fts_query, extract_title_and_snippet

from . import ranking, search_primitives, tags, validation
from ._shared import logger

# metadata_filter keys are interpolated into a json_extract('$.<key>') SQL path -- only the
# value is bound as a parameter, so the key itself must be allowlisted before interpolation
# to prevent SQL predicate injection (e.g. a key like "safe') OR 1=1 OR json_extract(e.metadata, '$.safe").
_METADATA_FILTER_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


def search_memory(  # noqa: C901, PLR0912, PLR0915
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
    context_id: str = None,
    agent_session_id: str = None,
    is_core: bool = None,
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"] = None,
    tag_operator: Literal["AND", "OR"] = "AND",
    cursor: str = None,
    include_related: bool = True,
    prefer_durable_types: bool = False,
    demote_superseded: bool = False,
    cross_encoder_candidate_cap: int | None = None,
    cross_encoder_text_cap_chars: int | None = None,
    use_chunk_candidates: bool = False,
    oversampling_multiplier: int | None = None,
    candidate_window: int | None = None,
    chunk_weight: float | None = None,
    collapse_supersedes_families: bool = False,
    return_diagnostics: bool = False,
    mode: Literal["strict", "broad", "history"] = "broad",
    disable_semantic: bool = False,
    use_retrieval_text_candidates: bool = False,
    retrieval_fts_weight: float | None = None,
    retrieval_vector_weight: float | None = None,
    db_connection=None,
    db_path: str = None,
) -> list | dict:
    """Performs full-text keyword search and filtering in long-term memory.

    disable_semantic (default False; Track B, scratch/plans/track_b_daemon_detailed.md §14): a
    per-call override forcing the FTS-only path for this one request, regardless of the
    SALTMDB_ENABLE_SEMANTIC env var -- added because a persistent daemon reads its environment
    once at its own startup and holds it fixed, so a caller-side env mutation (as
    `cmd_bootstrap_digest`'s `--no-semantic` flag used to do) has no effect on an already-running
    daemon. Evaluated fresh on every call, never a global/env mutation, since the daemon is
    multi-threaded and a shared mutable flag would race concurrent calls. Governs only this
    function's own semantic-search gate below -- `check_duplicate_memories`'s separate
    `is_semantic_search_enabled()` call site is unrelated and unaffected.

    Cross-encoder reranking (roadmap `ba2cf66f` P1#7, design `1fddc04a`/`8115fa4a`) occupies a
    fixed Stage-2 final-reranker slot -- there is no caller-facing enable/force flag. When the
    deployment configures a supported `SALTMDB_RERANKER_MODEL`, the widened pool is scored by the
    ONNX cross-encoder (`reranker_service.score_pairs`, no PyTorch runtime) and its ordering fully
    overrides RRF for scored candidates. The stage deliberately bypasses `_rrf_gap_confident`;
    the configured model is the final judge even for a decisive dual-channel RRF winner.
    Deterministic fallback: an unset/unsupported model or any runner failure leaves
    `ranked_pool_` exactly as RRF produced it -- no exception and no widened result count.
    Cross-encoder scores are
    attached to `accept_or_abstain`'s evidence dict as an inert `cross_encoder_score` field this
    release -- they do NOT affect the accept/reject decision yet (that requires its own future,
    separately-calibrated gate rule, not an uncalibrated one invented here).

    mode (opt-in, default "broad"): Part C of plans/scalable-strolling-stallman.md (SALTMDB
    memory `9c199005`).
    - "broad": no chain resolution, no relevance gate, no `is_superseded` tagging. A literal
      title matching exactly one active entity within the caller's filters uses an identity fast
      path and returns that entity without running hybrid retrieval; title collisions retain the
      ordinary hybrid order. Explicit `collapse_supersedes_families=True` takes precedence over
      identity matching and stays on the ordinary pipeline so its canonical-family contract holds.
    - "strict": matched-but-superseded candidates are resolved and SUBSTITUTED with their live,
      multi-hop-revalidated `supersedes` successor (Part A); every surviving candidate must then
      independently clear a calibrated relevance-abstention gate (Part B) or is dropped. An empty
      result (`[]`) is a normal, successful outcome for a query with no sufficiently-grounded
      match -- not an error. Widens the candidate pool the same way the mandatory cross-encoder
      stage/prefer_durable_types/demote_superseded already do, and retries with a larger pool (up to
      STRICT_OVERFETCH_CANDIDATE_CAP) when resolution/dedup/the gate shrink the post-policy pool
      below what `offset`+`limit` needs (Part C2 -- pagination continuity across a rejection,
      substitution, or many-to-one dedup collapse). Additionally, unconditionally and regardless
      of the `prefer_durable_types`/`demote_superseded` flags above: durable-type preference is
      always applied, and a surviving candidate is demoted (never excluded -- it already cleared
      the gate on its own evidence) if it's still the target of a currently-valid `supersedes`
      edge Part A's resolver couldn't cleanly resolve (cycle/depth-cap/archived-intermediate
      abstain) or of a currently-valid `corrects` edge (roadmap `ba2cf66f` P1#6, design
      `1fddc04a`; `_apply_strict_ranking_defaults`).
    - "history": no resolution, no gate -- every live candidate the hybrid pipeline would
      otherwise return stays visible exactly as in "broad", except a candidate that is the target
      of a currently-valid `supersedes` edge is additionally tagged `"is_superseded": true` in its
      result item. Does NOT relax entity-status visibility: every mode still starts from
      `e.status != 'archived'`; "history" only stops live-but-superseded memories from being
      silently hidden, it never exposes archived material.
    `mode` only applies to the query-keyword-based hybrid pipeline -- it has no effect on
    `explain_mode` (which returns before retrieval) or on empty-query filter/tag-only browsing
    (there is no retrieval evidence to gate or chain to resolve there).
    """
    if mode not in ("strict", "broad", "history"):
        logger.warning("search_memory: unknown mode=%r, falling back to 'broad'.", mode)
        mode = "broad"

    # candidate/search-ce-final-reranker (merged d1655d2): the cross-encoder occupies the fixed
    # final-reranker slot and is no longer a caller opt-in. When enabled by deployment config it
    # replaces RRF as the caller-visible order and bypasses _rrf_gap_confident, so CE gets the
    # final say on every eligible query, not just ambiguous ones. The old full-pool topic-rerank
    # path is retired. score_pairs() returning None (disabled/error/malformed) still leaves
    # ranked_pool_ exactly as RRF produced it.

    chunk_oversampling, chunk_window, configured_chunk_weight = (
        validation._validate_chunk_candidate_controls(
            use_chunk_candidates, oversampling_multiplier, candidate_window, chunk_weight
        )
    )
    if collapse_supersedes_families and mode != "broad":
        raise ValueError("collapse_supersedes_families is supported only in broad mode")
    ce_candidate_cap, ce_text_cap = validation._validate_cross_encoder_controls(
        cross_encoder_candidate_cap,
        cross_encoder_text_cap_chars,
        enabled=True,
    )
    configured_retrieval_fts_weight, configured_retrieval_vector_weight = (
        validation._validate_retrieval_text_controls(
            use_retrieval_text_candidates,
            retrieval_fts_weight,
            retrieval_vector_weight,
        )
    )
    diagnostics: dict[str, Any] = {
        "use_chunk_candidates": bool(use_chunk_candidates),
        "chunk_candidate": {
            "requested": bool(use_chunk_candidates),
            "oversampling_multiplier": chunk_oversampling,
            "candidate_window": chunk_window,
            "chunk_weight": configured_chunk_weight,
            "candidate_shortfall": 0,
            "executed": False,
        },
        "collapse_supersedes_families": bool(collapse_supersedes_families),
        "retrieval_text": {
            "requested": bool(use_retrieval_text_candidates),
            "retrieval_fts_weight": configured_retrieval_fts_weight,
            "retrieval_vector_weight": configured_retrieval_vector_weight,
            "fts_candidate_count": 0,
            "vector_candidate_count": 0,
            "candidate_evidence": {},
            "executed": False,
        },
        "cross_encoder": {
            "requested": True,
            "forced": True,
            "candidate_cap": ce_candidate_cap,
            "text_cap_chars": ce_text_cap,
            "executed": False,
            "execution_count": 0,
            "reason": None,
        },
    }

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    offset = 0
    if cursor and cursor.startswith("offset:"):
        try:
            offset = int(cursor.split(":")[1])
        except ValueError:
            pass

    try:
        where_clauses = ["e.status != 'archived'"]
        params: list[Any] = []  # mixed str/int SQL bind values (e.g. is_core -> 0/1)

        if owner_id:
            where_clauses.append("(e.owner_id = ? OR e.scope = 'shared')")
            params.append(owner_id)

        if context_id:
            where_clauses.append(
                "(e.context_id = ? OR json_extract(e.metadata, '$.project') = ? OR json_extract(e.metadata, '$.project_id') = ?)"
            )
            params.extend([context_id, context_id, context_id])

        if agent_session_id:
            where_clauses.append("e.agent_session_id = ?")
            params.append(agent_session_id)

        if is_core is not None:
            where_clauses.append("e.is_core = ?")
            params.append(1 if is_core else 0)

        if memory_type_filter is not None:
            where_clauses.append("e.memory_type = ?")
            params.append(memory_type_filter)

        if metadata_filter and isinstance(metadata_filter, dict):
            for mk, mv in metadata_filter.items():
                if not _METADATA_FILTER_KEY_RE.match(mk):
                    raise ValueError(
                        f"metadata_filter key {mk!r} is invalid; keys must match "
                        f"{_METADATA_FILTER_KEY_RE.pattern!r}"
                    )
                where_clauses.append(f"json_extract(e.metadata, '$.{mk}') = ?")
                params.append(str(mv))

        if tags_filter:
            norm_tags = [tags.normalize_tag_name(t) for t in tags_filter if t.strip()]
            if norm_tags:
                tag_groups = []
                for tname in norm_tags:
                    grp = set()
                    c = conn.execute(
                        "SELECT id, canonical_id FROM tags WHERE lower(name) = lower(?)", (tname,)
                    )
                    for tid, tcanon in c.fetchall():
                        grp.add(tid)
                        main_id = tcanon if tcanon else tid
                        grp.add(main_id)
                        alias_c = conn.execute(
                            "SELECT id FROM tags WHERE canonical_id = ?", (main_id,)
                        )
                        for ar in alias_c.fetchall():
                            grp.add(ar[0])
                    tag_groups.append((tname, grp))

                if tag_operator == "AND":
                    for tname, grp in tag_groups:
                        if grp:
                            placeholders = ",".join("?" for _ in grp)
                            where_clauses.append(
                                f"e.id IN (SELECT et.entity_id FROM entity_tags et WHERE et.tag_id IN ({placeholders}))"
                            )
                            params.extend(list(grp))
                        else:
                            where_clauses.append(
                                "e.id IN (SELECT et.entity_id FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE lower(t.name) = lower(?))"
                            )
                            params.append(tname)
                else:
                    all_ids = set()
                    missing_tnames = []
                    for tname, grp in tag_groups:
                        if grp:
                            all_ids.update(grp)
                        else:
                            missing_tnames.append(tname)

                    sub_clauses = []
                    sub_params = []
                    if all_ids:
                        placeholders = ",".join("?" for _ in all_ids)
                        sub_clauses.append(f"et.tag_id IN ({placeholders})")
                        sub_params.extend(list(all_ids))
                    if missing_tnames:
                        placeholders = ",".join("?" for _ in missing_tnames)
                        sub_clauses.append(
                            f"lower(t.name) IN ({','.join('lower(?)' for _ in missing_tnames)})"
                        )
                        sub_params.extend(missing_tnames)

                    if sub_clauses:
                        where_clauses.append(
                            f"e.id IN (SELECT et.entity_id FROM entity_tags et LEFT JOIN tags t ON et.tag_id = t.id WHERE {' OR '.join(sub_clauses)})"
                        )
                        params.extend(sub_params)

        sanitized_query = sanitize_fts_query(query_keywords) if query_keywords else ""

        if explain_mode:
            if mode != "broad":
                logger.debug("mode=%r ignored: explain_mode takes precedence.", mode)
            terms = sanitized_query.split() if sanitized_query else []
            searched_terms = {}
            for t in terms:
                c = conn.execute(
                    "SELECT 1 FROM entities_fts WHERE entities_fts MATCH ?", (f'"{t}"*',)
                ).fetchone()
                searched_terms[t] = bool(c)

            invalid_tags = []
            if tags_filter:
                for tf in tags_filter:
                    tname = tags.normalize_tag_name(tf)
                    c = conn.execute(
                        "SELECT 1 FROM tags WHERE lower(name) = lower(?)", (tname,)
                    ).fetchone()
                    if not c:
                        invalid_tags.append(tf)

            explain_result = {
                "explain": {
                    "searched_terms_found": searched_terms,
                    "invalid_tags_suggestions": invalid_tags,
                    "sanitized_query": sanitized_query,
                    "where_clauses": where_clauses,
                }
            }
            if return_diagnostics:
                explain_result["diagnostics"] = diagnostics
            validation._set_search_diagnostics(diagnostics)
            return explain_result

        exact_title_id = None
        if query_keywords and mode in {"broad", "history"}:
            # A partial index on active titles keeps this bounded lookup independent of corpus
            # size. LIMIT 2 distinguishes a unique visible identity from a collision without
            # loading every duplicate; existing caller filters define the visible corpus.
            exact_title_rows = conn.execute(
                f"SELECT e.id FROM entities e WHERE e.title = ? "
                f"AND {' AND '.join(where_clauses)} LIMIT 2",
                [query_keywords, *params],
            ).fetchall()
            if len(exact_title_rows) == 1:
                exact_title_id = exact_title_rows[0][0]

        rows: list[Any] = []
        # topic_score/semantic_verdict are never exposed in result items -- the topic-rerank
        # full-override path is retired (see the docstring/forcing-block comments above), and
        # mode="strict"'s own on-demand topic scoring (below) is relevance-gate evidence only,
        # never surfaced to the caller.
        cross_encoder_scores_map: dict[str, float] = {}
        # Populated only under mode="history" -- ids in the final pool that are the target of a
        # currently-valid `supersedes` edge, tagged (not hidden/reordered) in the result item.
        superseded_ids: set = set()
        if exact_title_id is not None and not collapse_supersedes_families:
            if offset == 0 and limit > 0:
                rows = conn.execute(
                    """
                    SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                           0.0 as rank_score,
                           e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata,
                           e.context_id, e.memory_type, 0 as rel_count, NULL as fts_snippet
                    FROM entities e
                    WHERE e.id = ?
                    """,
                    (exact_title_id,),
                ).fetchall()
                if mode == "history":
                    superseded_ids = ranking._compute_superseded_ids_bitemporal(
                        [exact_title_id], conn
                    )
        elif sanitized_query:
            assert query_keywords  # nosec B101 -- mypy narrowing only, not a runtime safety check
            from saltmdb.config import is_semantic_search_enabled

            if is_semantic_search_enabled() and not disable_semantic:
                if not db_path:
                    db_path = get_db_path()

                def _compute_pool(candidate_window: int) -> dict:  # noqa: C901, PLR0912, PLR0915
                    """One full FTS+semantic+RRF-fuse+[resolve+substitute]+[rerank]+[gate]+
                    [ranking-flags] pass at a given candidate_window size (Part C pipeline
                    ordering: RRF fusion -> gap-gate check off the ORIGINAL un-substituted sets ->
                    chain-resolution/substitution (mode="strict") -> cross-encoder rerank
                    (unconditional, bypasses the gap gate) -> accept_or_abstain filter over the full
                    widened pool (mode="strict"), before offset/limit slicing -> mark superseded
                    (mode="history") -> prefer_durable_types -> demote_superseded). Returns the
                    final ordered candidate id list (not yet offset/limit-sliced) plus enough
                    metadata for the mode="strict" overfetch retry loop below (Part C2) to decide
                    whether widening further could help, and for row assembly afterward.
                    """
                    fts_rows_, used_or_fallback_ = search_primitives._run_fts_search(
                        conn,
                        sanitized_query,
                        where_clauses,
                        params,
                        candidate_window,
                        0,
                        return_fallback_flag=True,
                    )
                    semantic_rows_ = search_primitives.semantic_search(
                        query_keywords, where_clauses, params, candidate_window, db_path, 0
                    )
                    chunk_rows_: list[tuple[str, float]] = []
                    chunk_diagnostics_: dict[str, Any] = {
                        "requested_chunk_rows": 0,
                        "candidate_window": chunk_window,
                        "oversampling_multiplier": chunk_oversampling,
                        "raw_chunk_rows": 0,
                        "unique_fresh_entities": 0,
                        "returned_entities": 0,
                        "candidate_shortfall": 0,
                        "executed": False,
                    }
                    if use_chunk_candidates:
                        chunk_rows_, chunk_diagnostics_ = search_primitives.chunk_candidate_search(
                            query_keywords,
                            where_clauses,
                            params,
                            chunk_window,
                            chunk_oversampling,
                            db_path,
                        )
                        chunk_diagnostics_["executed"] = True
                        diagnostics["chunk_candidate"] = dict(chunk_diagnostics_)

                    retrieval_fts_rows_: list[tuple[str, float]] = []
                    retrieval_vector_rows_: list[tuple[str, float]] = []
                    if use_retrieval_text_candidates:
                        retrieval_fts_rows_ = search_primitives._run_retrieval_fts_search(
                            conn,
                            sanitized_query,
                            where_clauses,
                            params,
                            candidate_window,
                            0,
                        )
                        retrieval_vector_rows_ = search_primitives.retrieval_vector_search(
                            query_keywords,
                            where_clauses,
                            params,
                            candidate_window,
                            db_path,
                            0,
                        )
                        retrieval_diag = diagnostics["retrieval_text"]
                        retrieval_diag["executed"] = True
                        retrieval_diag["fts_candidate_count"] = len(retrieval_fts_rows_)
                        retrieval_diag["vector_candidate_count"] = len(retrieval_vector_rows_)
                        # Hash/presence/rank evidence is safe to expose; retrieval text itself is
                        # intentionally never selected or copied into diagnostics.
                        retrieval_ids = {row[0] for row in retrieval_fts_rows_} | {
                            row[0] for row in retrieval_vector_rows_
                        }
                        if retrieval_ids:
                            placeholders = ",".join("?" for _ in retrieval_ids)
                            hash_rows = conn.execute(
                                f"SELECT id,retrieval_text_hash FROM entities WHERE id IN ({placeholders})",
                                list(retrieval_ids),
                            ).fetchall()
                            fts_rank = {
                                row[0]: rank for rank, row in enumerate(retrieval_fts_rows_)
                            }
                            vector_rank = {
                                row[0]: rank for rank, row in enumerate(retrieval_vector_rows_)
                            }
                            retrieval_diag["candidate_evidence"] = {
                                eid: {
                                    "present": True,
                                    "hash": next((h for i, h in hash_rows if i == eid), None),
                                    "fts_rank": fts_rank.get(eid),
                                    "vector_rank": vector_rank.get(eid),
                                }
                                for eid in retrieval_ids
                            }

                    rrf_map = search_primitives.weighted_reciprocal_rank_fusion(
                        fts_rows_,
                        semantic_rows_,
                        chunk_rows_,
                        candidate_window,
                        chunk_weight=configured_chunk_weight,
                        retrieval_fts_results=retrieval_fts_rows_,
                        retrieval_vector_results=retrieval_vector_rows_,
                        retrieval_fts_weight=configured_retrieval_fts_weight,
                        retrieval_vector_weight=configured_retrieval_vector_weight,
                    )
                    if use_retrieval_text_candidates:
                        evidence = diagnostics["retrieval_text"].get("candidate_evidence", {})
                        for eid, details in evidence.items():
                            contribution = 0.0
                            if details.get("fts_rank") is not None:
                                contribution += configured_retrieval_fts_weight / (
                                    60 + details["fts_rank"] + 1
                                )
                            if details.get("vector_rank") is not None:
                                contribution += configured_retrieval_vector_weight / (
                                    60 + details["vector_rank"] + 1
                                )
                            details["rrf_contribution"] = round(contribution, 9)
                    fts_ids_ = {r[0] for r in fts_rows_}
                    # True-AND-only id set (H1 fix): empty whenever used_or_fallback_ is True,
                    # since every row in fts_rows_ then came from the OR-joined retry, not a
                    # genuine AND match -- see _run_fts_search's own docstring for why this is a
                    # single pool-level bool, not a per-row property.
                    fts_and_ids_ = fts_ids_ if not used_or_fallback_ else set()
                    semantic_ids_ = {eid for eid, _ in semantic_rows_}

                    resolved_from_: dict[str, list[str]] = {}
                    predecessor_grounded_map: dict[str, bool] = {}
                    if rrf_map and mode == "strict":
                        # Part A: resolve off the ORIGINAL, un-substituted pool -- gap-gate below
                        # also reads fts_ids_/semantic_ids_ pre-substitution, same invariant.
                        pre_pool_ids = list(rrf_map.keys())
                        resolved_map = ranking._resolve_supersession_chains(
                            conn, pre_pool_ids, where_clauses, params
                        )
                        if resolved_map:
                            # H1 predecessor-grounding fix: on-demand topic-score exactly the
                            # OR-fallback-only subset of pre_pool_ids (true-AND pre-pool candidates
                            # already have a sufficient DIRECT signal and skip this lookup) BEFORE
                            # building pre_evidence -- otherwise an OR-fallback-only predecessor can
                            # never get a semantic_verdict at all, silently always failing the
                            # in_fts_or_only rule below regardless of its real topic relevance.
                            pre_or_fallback_only_ids = (
                                [eid for eid in pre_pool_ids if eid in fts_ids_]
                                if used_or_fallback_
                                else []
                            )
                            pre_topic_scores_map = (
                                search_primitives._score_topics_with_fallback(
                                    query_keywords, pre_or_fallback_only_ids, db_path
                                )
                                if pre_or_fallback_only_ids
                                else {}
                            )
                            pre_evidence = ranking._build_candidate_evidence(
                                pre_pool_ids,
                                rrf_map,
                                fts_rows_,
                                semantic_rows_,
                                pre_topic_scores_map,
                                {},
                                used_or_fallback=used_or_fallback_,
                            )
                            for cid, head in resolved_map.items():
                                resolved_from_.setdefault(head, []).append(cid)
                            for head, preds in resolved_from_.items():
                                predecessor_grounded_map[head] = any(
                                    ranking.accept_or_abstain(pre_evidence[p])[0]
                                    for p in preds
                                    if p in pre_evidence
                                )
                            rrf_map = ranking._substitute_resolved_heads(rrf_map, resolved_map)

                    topic_scores_map_: dict[str, dict] = {}
                    cross_encoder_scores_map_: dict[str, float] = {}
                    if rrf_map:
                        # Part 1 gap gate (SALTMDB memory 870a1d4e): originally could skip Stage 2
                        # entirely when hybrid search already had a decisive, dual-channel-
                        # corroborated winner. The fixed cross-encoder stage below bypasses this
                        # gate whenever deployment configuration enables scoring, so the gate no
                        # longer skips execution -- it is kept only for the observability debug
                        # log below. Deliberately checked against the pre-substitution
                        # fts_ids_/semantic_ids_ sets (a resolved head's own channel membership is
                        # a separate, Part B evidence question, not this gate's).
                        # The v1 gap gate deliberately ignores chunk-only evidence.  It uses a
                        # legacy two-channel map so adding a chunk candidate can never make a
                        # previously ambiguous FTS/entity-vector query appear decisive.
                        gap_rrf_map = search_primitives.reciprocal_rank_fusion(
                            fts_rows_, semantic_rows_, candidate_window
                        )
                        gap_confident = ranking._rrf_gap_confident(
                            gap_rrf_map, fts_ids_, semantic_ids_
                        )
                        if gap_confident:
                            logger.debug(
                                "Stage-2 rerank gap already decisive (dual-channel top1, ratio >= "
                                "RERANK_GAP_SKIP_RATIO), but cross-encoder still runs (forced)."
                            )
                        ranked_pool_ = list(rrf_map.keys())
                        if mode == "strict":
                            # accept_or_abstain's DIRECT semantic-only rule (Part B) needs a
                            # calibrated topic_verdict, not a raw distance (see its own
                            # docstring for why a distance/margin cutoff was tried and
                            # empirically rejected) -- compute it on demand, WITHOUT reordering
                            # the pool, and only for candidates that actually need it: those
                            # lacking a genuine FTS AND-match (fts_and_ids_, NOT the broader
                            # fts_ids_ -- H1 fix). An OR-fallback-only candidate IS included here
                            # and DOES get a semantic_verdict computed, so accept_or_abstain's
                            # in_fts_or_only rule can actually be satisfied; only true-AND/
                            # dual-channel candidates already have a sufficient DIRECT signal and
                            # skip this lookup, keeping the added cost bounded.
                            ungrounded_ids = [
                                eid for eid in ranked_pool_ if eid not in fts_and_ids_
                            ]
                            topic_scores_map_ = search_primitives._score_topics_with_fallback(
                                query_keywords, ungrounded_ids, db_path
                            )

                        if ranked_pool_:
                            # The cross-encoder is the unconditional, always-on Stage-2 final
                            # reranker (see the forcing-block comment near the top of this
                            # function) -- it runs regardless of gap_confident.
                            from saltmdb.domain.services import reranker_service

                            ce_scores = None
                            if reranker_service.is_cross_encoder_enabled():
                                # Skip the batch fetch entirely when disabled/misconfigured --
                                # score_pairs would return None anyway, no point paying for the
                                # SQL round-trip first. Cap the pool to CROSS_ENCODER_MAX_CANDIDATES
                                # BEFORE the fetch (Codex full-diff review finding), not after --
                                # mode="strict"'s overfetch retry loop can widen ranked_pool_ up to
                                # STRICT_OVERFETCH_CANDIDATE_CAP (200), and fetching every one of
                                # those full documents just to discard all but the first 10 would
                                # be a pointless, potentially large, unbounded-with-corpus-growth
                                # SQL read for no benefit -- score_pairs itself caps input length
                                # regardless, so nothing downstream needs the wider fetch.
                                ce_pool_ids = list(ranked_pool_)[:ce_candidate_cap]
                                ce_text_by_id = ranking._build_cross_encoder_candidate_texts(
                                    query_keywords, ce_pool_ids, conn, db_path
                                )

                                scored_ids_in_order = [
                                    eid for eid in ce_pool_ids if eid in ce_text_by_id
                                ]
                                ce_texts = [ce_text_by_id[eid] for eid in scored_ids_in_order]
                                score_kwargs = {}
                                if cross_encoder_candidate_cap is not None:
                                    score_kwargs["candidate_cap"] = ce_candidate_cap
                                if cross_encoder_text_cap_chars is not None:
                                    score_kwargs["text_cap_chars"] = ce_text_cap
                                ce_scores = reranker_service.score_pairs(
                                    query_keywords, ce_texts, **score_kwargs
                                )
                            if ce_scores is not None:
                                cross_encoder_scores_map_ = dict(
                                    zip(scored_ids_in_order, ce_scores)
                                )
                                reordered = sorted(
                                    scored_ids_in_order,
                                    key=lambda eid: -cross_encoder_scores_map_[eid],
                                )
                                unscored_tail = [
                                    eid
                                    for eid in ranked_pool_
                                    if eid not in cross_encoder_scores_map_
                                ]
                                ranked_pool_ = reordered + unscored_tail
                                ce_diagnostics = reranker_service.get_last_score_diagnostics()
                                diagnostics["cross_encoder"].update(ce_diagnostics)
                                diagnostics["cross_encoder"]["executed"] = True
                                diagnostics["cross_encoder"]["execution_count"] = (
                                    diagnostics["cross_encoder"].get("execution_count", 0) + 1
                                )
                            else:
                                ce_diagnostics = reranker_service.get_last_score_diagnostics()
                                diagnostics["cross_encoder"].update(ce_diagnostics)
                            # ce_scores is None (disabled/unsupported model/runner failure/
                            # malformed output): ranked_pool_ is left exactly as it was before this
                            # block -- deterministic fallback to current behavior, no widening.

                        superseded_ids_: set = set()
                        if mode == "strict":
                            evidence_map = ranking._build_candidate_evidence(
                                ranked_pool_,
                                rrf_map,
                                fts_rows_,
                                semantic_rows_,
                                topic_scores_map_,
                                resolved_from_,
                                predecessor_grounded_map,
                                cross_encoder_scores_map_,
                                used_or_fallback=used_or_fallback_,
                            )
                            accepted_pool = []
                            for eid in ranked_pool_:
                                ok, reason = ranking.accept_or_abstain(evidence_map[eid])
                                logger.debug(
                                    "search_memory strict gate: %s -> accept=%s (%s)",
                                    eid,
                                    ok,
                                    reason,
                                )
                                if ok:
                                    accepted_pool.append(eid)
                            ranked_pool_ = accepted_pool
                        elif mode == "history":
                            superseded_ids_ = ranking._compute_superseded_ids_bitemporal(
                                ranked_pool_, conn
                            )

                        # Part 2 (SALTMDB memory 870a1d4e): type bias first, then supersession
                        # demotion -- ensures an explicitly-superseded item always sinks below a
                        # merely-event-typed one, not the reverse. Applied to the FULL pool,
                        # before the offset/limit slice.
                        if prefer_durable_types:
                            ranked_pool_ = ranking._apply_type_bias(ranked_pool_, conn)
                        if demote_superseded:
                            ranked_pool_ = ranking._apply_supersession_demotion(ranked_pool_, conn)
                        # Roadmap ba2cf66f P1#6 / design 1fddc04a: durable-type preference and a
                        # supersession/correction safety-net demotion are forced, unconditional
                        # defaults under mode="strict", independent of the two independently-togglable flags above
                        # (which keep their existing, narrower, mode-agnostic meaning and may have
                        # already run a second time here -- harmless, a stable partition on the
                        # same criterion applied twice is a no-op the second time). broad/history
                        # are completely unreached by this branch -- their pre-existing behavior is
                        # byte-identical, unaffected by this addition.
                        if mode == "strict":
                            ranked_pool_ = ranking._apply_strict_ranking_defaults(
                                ranked_pool_, conn
                            )
                        elif mode == "broad" and collapse_supersedes_families:
                            # Collapse only after all existing broad-mode ordering flags.  The
                            # head is already in rrf_map and therefore retains its own score and
                            # row; this merely moves that existing id to the first family-member
                            # position and skips later members.
                            before_collapse = len(ranked_pool_)
                            ranked_pool_ = ranking._collapse_supersedes_families(ranked_pool_, conn)
                            diagnostics["supersedes_collapse"] = {
                                "executed": True,
                                "before_count": before_collapse,
                                "after_count": len(ranked_pool_),
                            }
                    else:
                        ranked_pool_ = []
                        superseded_ids_ = set()

                    # Both raw channels returned fewer rows than requested -> the underlying
                    # corpus is exhausted for this query at this window size; growing
                    # candidate_window further cannot reveal more candidates (Part C2).
                    exhausted_ = (
                        len(fts_rows_) < candidate_window
                        and len(semantic_rows_) < candidate_window
                        and (
                            not use_retrieval_text_candidates
                            or (
                                len(retrieval_fts_rows_) < candidate_window
                                and len(retrieval_vector_rows_) < candidate_window
                            )
                        )
                    )
                    return {
                        "ordered_ids": ranked_pool_,
                        "fts_rows": fts_rows_,
                        "cross_encoder_scores_map": cross_encoder_scores_map_,
                        "superseded_ids": superseded_ids_,
                        "exhausted": exhausted_,
                        # Post-substitution RRF fusion scores (Part A dedup-merge already applied
                        # by _substitute_resolved_heads when mode="strict") -- used for the result
                        # item's own "score" field below, same as before this refactor: the
                        # assembled item's score is always the RRF fusion score, even when the
                        # cross-encoder stage reordered `ordered_ids` (cross_encoder_score is
                        # attached separately, it never replaces this field).
                        "rrf_score_map": rrf_map,
                        "chunk_diagnostics": chunk_diagnostics_,
                    }

                # Widen the pool: the cross-encoder is unconditionally always on (see the
                # forcing-block comment near the top of this function), so this branch is now
                # always taken -- there's nothing meaningful to reorder/resolve/gate within
                # otherwise (a plain search's pool is just offset+limit, often smaller than
                # what's worth considering).
                from saltmdb.config import RERANK_CANDIDATE_POOL_SIZE

                base_window = max(offset + limit, RERANK_CANDIDATE_POOL_SIZE)

                if mode == "strict":
                    # Part C2 pagination redesign: resolution/dedup/the relevance gate can all
                    # shrink the raw candidate_window's survivor count below offset+limit even
                    # after the widening above. Re-run the WHOLE pass (from scratch, same
                    # candidate_window semantics as every other mode -- see _run_fts_search's own
                    # LIMIT candidate_window OFFSET 0, then this function's own final
                    # `[offset:offset+limit]` Python slice below) with a doubled window until
                    # enough survivors exist, the underlying corpus is exhausted, or the cap is
                    # hit. Because every pass recomputes the full pool deterministically from
                    # scratch (not an incremental DB offset), a later cursor call with a larger
                    # `offset` reproduces a stable superset of this same computation -- cursor
                    # continuity across a rejection/substitution/dedup collapse holds as long as
                    # the underlying corpus doesn't change between calls, exactly like this
                    # function's pre-existing offset:N cursor already assumed for every other mode.
                    # No early "no-progress" stop here (Codex review P1 finding, correctly
                    # rejected during re-review): an earlier version broke out after a single
                    # doubling found zero additional accepted survivors, on the theory that a
                    # genuinely relevant query should keep surfacing more matches as the window
                    # grows. That's false in general -- real accepted candidates can legitimately
                    # sit beyond the NEXT window too (e.g. ranks 41-80 when the window just grew
                    # from 20 to 40), so that guard could return a prematurely short/empty page for
                    # a genuinely satisfiable query. The plan's own spec is exactly "grow until
                    # enough survivors, exhaustion, or cap" -- STRICT_OVERFETCH_CANDIDATE_CAP is
                    # already the sole, deliberate safety valve on how far this is allowed to
                    # search (see its own config.py docstring); a nonsense query scanning further
                    # toward that cap in search of a real match is the accepted, documented
                    # trade-off (see accept_or_abstain's docstring and
                    # run_relevance_gate_holdout.py's held-out cases), not a bug to work around
                    # with a second, undocumented early-exit heuristic.
                    # Clamp the STARTING window to the cap too (Codex review round-2 P2 finding):
                    # base_window is max(offset+limit, RERANK_CANDIDATE_POOL_SIZE), which can
                    # itself already exceed STRICT_OVERFETCH_CANDIDATE_CAP for a large `limit` or a
                    # deep `offset` cursor -- the loop's own `window < CAP` condition only guards
                    # the DOUBLING step, not this initial value, so without this clamp the very
                    # first _compute_pool() call could silently run past the "absolute cap" the
                    # config/docs promise.
                    window = min(base_window, STRICT_OVERFETCH_CANDIDATE_CAP)
                    pool_result = _compute_pool(window)
                    while (
                        len(pool_result["ordered_ids"]) < offset + limit
                        and not pool_result["exhausted"]
                        and window < STRICT_OVERFETCH_CANDIDATE_CAP
                    ):
                        window = min(window * 2, STRICT_OVERFETCH_CANDIDATE_CAP)
                        pool_result = _compute_pool(window)
                else:
                    pool_result = _compute_pool(base_window)

                fts_rows = pool_result["fts_rows"]
                cross_encoder_scores_map = pool_result["cross_encoder_scores_map"]
                superseded_ids = pool_result["superseded_ids"]
                rrf_score_map = pool_result["rrf_score_map"]
                merged_ids = pool_result["ordered_ids"][offset : offset + limit]

                if merged_ids:
                    placeholders = ",".join("?" for _ in merged_ids)
                    id_order = {eid: i for i, eid in enumerate(merged_ids)}
                    fetch_sql = f"""
                        SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                               0.0 as rank_score,
                               e.created_at, e.updated_at, e.owner_id, e.scope,
                               e.metadata, e.context_id, e.memory_type, 0 as rel_count,
                               NULL as fts_snippet
                        FROM entities e
                        WHERE e.id IN ({placeholders})
                    """
                    fetched = conn.execute(fetch_sql, merged_ids).fetchall()
                    sorted_fetched = sorted(fetched, key=lambda r: id_order.get(r[0], 9999))
                    # Rows that matched via FTS5 already carry a real query-centered excerpt in
                    # fts_rows (computed in the same query as bm25()); rows that only surfaced via
                    # semantic_search() never went through entities_fts MATCH at all, so they keep
                    # fts_snippet = None here and fall back to the heuristic extractor below.
                    fts_snippet_map = {row[0]: row[-1] for row in fts_rows if row[-1]}
                    rows = []
                    for r in sorted_fetched:
                        r_list = list(r)
                        r_list[5] = rrf_score_map.get(r[0], 0.0)
                        r_list[-1] = fts_snippet_map.get(r[0])
                        rows.append(r_list)
                else:
                    rows = []
            else:
                # Part 0 (SALTMDB memory 870a1d4e follow-on): FTS-only query retrieval is
                # retired, not silently substituted -- search_memory is a hybrid FTS+dense-vector
                # tool, and returning lower-quality FTS-only results as if nothing changed hid a
                # real precision regression from the caller. Fails loud via the function's own
                # existing except-Exception handler below instead. Empty-query browsing (no
                # query_keywords, the final `else` further down) is unaffected -- it never reaches
                # this branch.
                raise RuntimeError(
                    "Semantic search is disabled (SALTMDB_ENABLE_SEMANTIC=false); search_memory "
                    "requires the hybrid FTS+dense-vector pipeline for query-based search. Unset "
                    "SALTMDB_ENABLE_SEMANTIC (or set it to true), or call search_memory without "
                    "query_keywords to browse via tags/filters only."
                )
        else:
            sql = f"""
                SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                       0.0 as rank_score,
                       e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata, e.context_id,
                       e.memory_type, 0 as rel_count, NULL as fts_snippet
                FROM entities e
                WHERE {" AND ".join(where_clauses)}
                ORDER BY e.is_core DESC, e.updated_at DESC
                LIMIT ? OFFSET ?
            """
            exec_params = params + [limit, offset]
            cursor_obj = conn.execute(sql, exec_params)
            rows = cursor_obj.fetchall()

        diagnostics["result_count"] = len(rows)
        diagnostics["cross_encoder"]["execution_rate"] = (
            1.0 if diagnostics["cross_encoder"].get("execution_count", 0) > 0 else 0.0
        )
        validation._set_search_diagnostics(diagnostics)

        # Batch-fetch all related entities in a single query to avoid N+1. Ordering invariant:
        # this always runs on the FINAL rows/merged_ids-derived set -- Part B's candidate-window
        # widening and Stage-2 rerank both happen strictly before merged_ids is computed above, so
        # the wider pre-rerank pool never reaches this step. A future refactor that moves the
        # widening later could break this silently -- keep it upstream of this block.
        related_map: dict[str, list[Any]] = {}  # {entity_id: [related items]}
        if include_related and rows:
            all_eids = [r[0] for r in rows]
            placeholders_r = ",".join("?" for _ in all_eids)
            # Two unambiguous, direction-explicit selects (unioned) rather than one OR-join:
            # an OR-join (`ON (r.target_id = e.id OR r.source_id = e.id)`) matches an anchor
            # entity against itself whenever both endpoints of a relation are on the current
            # result page, and previously relied on `e.id NOT IN (all_eids)` to discard that
            # self-match -- which also silently discarded the case we actually want (two
            # co-resident page entities correctly showing each other as related). Each half of
            # this union resolves deterministically to the *other* endpoint, so co-resident
            # partners are now surfaced correctly and a self-match can never occur.
            batch_rel_cursor = conn.execute(
                f"""
                SELECT r.source_id AS anchor, r.predicate, e.id, e.title
                FROM relations r
                JOIN entities e ON e.id = r.target_id
                WHERE r.source_id IN ({placeholders_r})
                  AND e.status != 'archived'
                  AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
                UNION ALL
                SELECT r.target_id AS anchor, r.predicate, e.id, e.title
                FROM relations r
                JOIN entities e ON e.id = r.source_id
                WHERE r.target_id IN ({placeholders_r})
                  AND e.status != 'archived'
                  AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
            """,
                all_eids * 2,
            )
            for anchor, bpred, beid, betitle in batch_rel_cursor.fetchall():
                if len(related_map.get(anchor, [])) < 5:
                    related_map.setdefault(anchor, []).append(
                        {"predicate": bpred, "id": beid, "title": betitle}
                    )

        results = []
        for r in rows:
            (
                eid,
                etitle,
                econtent,
                eweight,
                eis_core,
                score,
                created,
                updated,
                owner,
                scope,
                meta,
                ctx,
                ememory_type,
                rel_c,
                fts_snippet_raw,
            ) = r
            drift_flag = None
            if meta:
                try:
                    meta_dict = json.loads(meta)
                    if isinstance(meta_dict, dict):
                        drift_flag = meta_dict.get("drift_flag")
                except (json.JSONDecodeError, TypeError):
                    pass

            if fts_snippet_raw:
                snippet = fts_snippet_raw
            else:
                _, snippet = extract_title_and_snippet(econtent)

            item = {
                "id": eid,
                "title": etitle,
                "snippet": snippet,
                "score": round(abs(score), 6),
                "weight": eweight,
                "is_core": bool(eis_core),
                "memory_type": ememory_type,
                "cursor": f"offset:{offset + limit}",
            }
            if include_related:
                item["related_entities"] = related_map.get(eid, [])
            if eid in cross_encoder_scores_map:
                item["cross_encoder_score"] = round(cross_encoder_scores_map[eid], 6)
            if mode == "history" and eid in superseded_ids:
                item["is_superseded"] = True
            if drift_flag:
                item["drift_flag"] = drift_flag

            results.append(item)

        if return_diagnostics:
            return {"results": results, "diagnostics": diagnostics}
        return results
    except Exception as e:
        logger.error("Error searching memory: %s", e)
        diagnostics["error"] = str(e)
        validation._set_search_diagnostics(diagnostics)
        if return_diagnostics:
            return {"results": [], "diagnostics": diagnostics, "error": str(e)}
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
