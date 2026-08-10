"""Track A: store-time disposition rewrite (memory-core rework, see
scratch/plans/track_a_disposition_detailed.md and the reconciliation doc it elaborates,
scratch/plans/rework_v2_daemon_and_disposition.md §1).

Replaces the old async Librarian queue (`consolidate_vector_clusters`,
`scout_consolidated_supersessions`) and `memory_service._handle_supersession_candidate` -- all
retired outright, no replacement flag/marker/queue of any kind -- with a synchronous, side-effect-
free preflight that runs as part of every `store_memory` call, plus an atomic disposition commit.

Two entry points, both plain functions taking a connection/context, not MCP-tool-coupled (so a
future daemon/RPC boundary move is wiring, not a rewrite):
- `evaluate_store_preflight` -- side-effect-free evidence-gathering + classification.
- `commit_disposed_write` -- the atomic write, only called once an agent has resolved every
  flagged candidate.

Import-graph note: this module imports `relation_service` and `memory_service.check_duplicate_
memories` at top level -- the same edge `relation_service.py` already establishes today
(`relation_service` -> `memory_service` at its own top level, proven safe). `memory_service.py`'s
own call into this module (from `store_memory`) MUST stay a deferred, in-function import, exactly
like its existing deferred imports of `librarian_service`/`embedding_service` -- a top-level import
here would create a real init-time cycle (memory_service -> disposition_service -> relation_service
-> memory_service, the last edge landing back on a not-yet-fully-defined module).
"""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta

from saltmdb.config import (
    DEDUP_DUPLICATE_THRESHOLD,
    DEDUP_SUPERSESSION_THRESHOLD,
    COHESION_MIN_PAIRWISE_THRESHOLD,
    MAX_CONSOLIDATION_REQUEST_SIZE,
    MAX_REVIEW_CANDIDATES,
    REVIEW_TOKEN_TTL_SECONDS,
)
from saltmdb.db.connection import write_transaction_retrying
from saltmdb.domain.services.cohesion_service import (
    compute_adhoc_centroid,
    get_fresh_entity_centroids,
    min_pairwise_cohesion,
)
from saltmdb.domain.services.event_service import log_event
from saltmdb.domain.services.memory_service import check_duplicate_memories
from saltmdb.domain.services.relation_service import commit_consolidation, store_relation
from saltmdb.utils.nlp import detect_correction_language
from saltmdb.utils.text import compute_content_hash

logger = logging.getLogger(__name__)

_CORE_SAFE_DISPOSITIONS = ["distinct", "supersede", "elaborate"]
_NON_CORE_DISPOSITIONS = ["distinct", "supersede", "consolidate"]
_HEURISTIC_NOTE = (
    "heuristic suggestion based on similarity/language signals -- not a determination; use your "
    "own judgment on the actual content, including calling this a false alarm."
)


class _ReviewStale(Exception):
    """Raised inside commit_disposed_write's in-transaction closure to abort+rollback on a stale
    revalidation; caught just outside write_transaction_retrying and converted to the
    REVIEW_STALE dict. Never escapes this module."""

    def __init__(self, stale_ids: list[str]):
        self.stale_ids = stale_ids
        super().__init__(f"stale candidates: {stale_ids}")


class _DispositionRejected(Exception):
    """Raised inside the same closure for a disposition-shape rejection discovered only once
    live is_core state is known (in-transaction) -- converted to a plain error string outside."""


# ---------------------------------------------------------------------------
# Proposed-write fingerprint + review token
# ---------------------------------------------------------------------------


def _canonical_write_payload(proposed: dict) -> dict:
    """The subset of `proposed` that affects persisted output or authorization -- everything the
    review token's fingerprint must bind to (Track A plan §1.3/§1.4, Codex round-2 fix: the
    round-1 draft omitted is_core/weight/metadata)."""
    tags = proposed.get("tags")
    return {
        "content": proposed.get("content"),
        "title": proposed.get("title"),
        "tags": sorted(tags) if tags else None,
        "scope": proposed.get("scope"),
        "memory_type": proposed.get("memory_type"),
        "context_id": proposed.get("context_id"),
        "owner_id": proposed.get("owner_id"),
        "is_core": proposed.get("is_core"),
        "weight": proposed.get("weight"),
        "metadata": proposed.get("metadata"),
    }


def _compute_write_fingerprint(proposed: dict) -> str:
    canonical = json.dumps(_canonical_write_payload(proposed), sort_keys=True, default=str)
    return compute_content_hash(canonical)


def _encode_review_token(payload: dict) -> str:
    """Opaque, base64url-encoded JSON -- deliberately NOT cryptographically signed. The security
    property this token needs ("a stale/forged claim about DB state is rejected") comes from
    live-DB revalidation at commit time (§1.3), not from token authenticity -- the caller is an
    already-trusted local agent with direct write access to this DB via every other tool, so
    signing would add key management for no real security property (Track A plan §1.4)."""
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_review_token(token: str) -> dict | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _check_consolidated_integrity(
    conn, proposed_content: str, target_id: str, db_path: str
) -> bool:
    """Returns True if the incoming content's centroid has drifted from a `consolidated`-status
    candidate target's own fresh centroid (below COHESION_MIN_PAIRWISE_THRESHOLD) -- the absorbed
    Phase 3 integrity check (Track A plan §2.2), evaluated inline, no periodic component.

    Neither centroid is a stored/recorded value -- both are computed fresh on every call
    (`get_fresh_entity_centroids` for the target, `compute_adhoc_centroid` for content that has no
    entity id yet), per the round-1 factual correction to this section.
    """
    adhoc_vec = compute_adhoc_centroid(proposed_content)
    if adhoc_vec is None:
        return False  # no embeddable content -- nothing to compare, not a signal either way

    centroids, unresolved, _observed_state = get_fresh_entity_centroids([target_id], conn, db_path)
    if target_id not in centroids:
        return False  # target unresolved this pass (archived/no content) -- not this check's job

    sim, _offending_pair = min_pairwise_cohesion({"__adhoc__": adhoc_vec, target_id: centroids[target_id]})
    return sim < COHESION_MIN_PAIRWISE_THRESHOLD


def evaluate_store_preflight(conn, proposed: dict, db_path: str) -> dict:  # noqa: C901, PLR0912
    """Side-effect-free. Returns {"candidates": [...]} -- an empty list means clear to store
    immediately, no REVIEW_REQUIRED gate.

    `proposed` keys: content, title, tags, owner_id, scope, memory_type, context_id, is_core,
    weight, metadata, resolved_entity_id (the entity id this write will target, or None for a
    fresh insert -- excluded from its own candidate evidence-gathering).
    """
    exclude_ids = [proposed["resolved_entity_id"]] if proposed.get("resolved_entity_id") else None
    dup_check = check_duplicate_memories(
        title=proposed.get("title"),
        content=proposed.get("content"),
        owner_id=proposed.get("owner_id"),
        tags=proposed.get("tags"),
        context_id=proposed.get("context_id"),
        exclude_ids=exclude_ids,
        db_connection=conn,
    )
    raw_candidates = dup_check.get("potential_duplicates") or []
    correction_phrases = detect_correction_language(proposed.get("content") or "")

    eligible = []
    for cand in raw_candidates:
        sim = cand.get("similarity_score", 0.0)
        if sim < DEDUP_SUPERSESSION_THRESHOLD:
            continue  # weak thematic similarity alone never triggers a flag

        row = conn.execute(
            "SELECT is_core, status, content_hash, memory_type, scope FROM entities WHERE id = ?",
            (cand["id"],),
        ).fetchone()
        if not row:
            continue
        target_is_core, target_status, target_content_hash, target_memory_type, target_scope = row

        proposed_type = proposed.get("memory_type")
        type_compatible = (
            proposed_type is None or target_memory_type is None or proposed_type == target_memory_type
        )
        scope_compatible = proposed.get("scope") == target_scope
        if not (type_compatible and scope_compatible):
            continue

        # Corrected during implementation review (Codex round-1 implementation-review finding
        # #1): the integrity check is ADDITIONAL to the normal B/C signal classification, not a
        # replacement for it -- reconciliation §1.2.3 says "Also check candidate neighbors that
        # are consolidated nodes for integrity signals," not "instead of." A consolidated target
        # that's ALSO a genuine high-overlap/correction-language match must still be flaggable as
        # possible_duplicate/possible_supersession, independent of whether it's also stale.
        bc_label = None
        bc_signals: list[str] = []
        if correction_phrases:
            bc_label = "possible_supersession"
            bc_signals = [f"correction_language:{p}" for p in correction_phrases]
        elif sim >= DEDUP_DUPLICATE_THRESHOLD:
            bc_label = "possible_duplicate"
            bc_signals = ["high_content_overlap"]

        is_stale = False
        if target_status == "consolidated":
            is_stale = _check_consolidated_integrity(
                conn, proposed.get("content") or "", cand["id"], db_path
            )

        if bc_label is None and not is_stale:
            continue  # neither signal class applies -- weak thematic similarity alone never flags

        if bc_label is not None:
            suggested_label = bc_label
            matched_signals = list(bc_signals)
            if is_stale:
                matched_signals.append("consolidated_node_cohesion_drift")
        else:
            suggested_label = "integrity_stale"
            matched_signals = ["consolidated_node_cohesion_drift"]

        available = list(_CORE_SAFE_DISPOSITIONS if target_is_core else _NON_CORE_DISPOSITIONS)
        eligible.append(
            {
                "target_entity_id": cand["id"],
                "target_title": cand.get("title"),
                "target_is_core": bool(target_is_core),
                "target_status": target_status,
                "target_content_hash": target_content_hash,
                "suggested_label": suggested_label,
                "heuristic_note": _HEURISTIC_NOTE,
                "evidence": {"similarity_score": sim, "matched_signals": matched_signals},
                "available_dispositions": available,
            }
        )

    eligible.sort(key=lambda c: c["evidence"]["similarity_score"], reverse=True)
    if len(eligible) > MAX_REVIEW_CANDIDATES:
        try:
            log_event(
                agent_id=proposed.get("owner_id") or "system",
                type="review_candidates_truncated",
                content=json.dumps(
                    {
                        "total_candidates": len(eligible),
                        "kept": MAX_REVIEW_CANDIDATES,
                        "title": proposed.get("title"),
                    }
                ),
                db_connection=conn,
                _in_transaction=True,
            )
        except Exception as e:  # audit-only -- never blocks the actual preflight
            logger.warning("Failed to log review_candidates_truncated event: %s", e)
        eligible = eligible[:MAX_REVIEW_CANDIDATES]

    for i, cand in enumerate(eligible, start=1):
        cand["candidate_id"] = f"c{i}"

    return {"candidates": eligible}


def build_review_required_response(proposed: dict, preflight: dict) -> dict:
    """Encodes the review token and shapes the REVIEW_REQUIRED dict `store_memory` returns."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=REVIEW_TOKEN_TTL_SECONDS)
    token_payload = {
        "v": 1,
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "proposed_write_fingerprint": _compute_write_fingerprint(proposed),
        "resolved_entity_id": proposed.get("resolved_entity_id"),
        "candidates": [
            {
                "candidate_id": c["candidate_id"],
                "target_entity_id": c["target_entity_id"],
                "target_content_hash": c["target_content_hash"],
                "target_status": c["target_status"],
                "target_is_core": c["target_is_core"],
                "available_dispositions": c["available_dispositions"],
            }
            for c in preflight["candidates"]
        ],
    }
    return {
        "status": "REVIEW_REQUIRED",
        "review_token": _encode_review_token(token_payload),
        "expires_at": expires_at.isoformat(),
        "candidates": [
            {
                "candidate_id": c["candidate_id"],
                "target_entity_id": c["target_entity_id"],
                "target_title": c["target_title"],
                "target_is_core": c["target_is_core"],
                "target_status": c["target_status"],
                "suggested_label": c["suggested_label"],
                "heuristic_note": c["heuristic_note"],
                "evidence": c["evidence"],
                "available_dispositions": c["available_dispositions"],
            }
            for c in preflight["candidates"]
        ],
    }


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def commit_disposed_write(  # noqa: C901, PLR0911, PLR0912, PLR0915
    conn, proposed: dict, review_token: str, dispositions: list, db_path: str
) -> dict | str:
    """Revalidates `review_token` against live DB state, then persists the write plus every
    chosen disposition's effect as one atomic transaction.

    Returns a `REVIEW_STALE` dict on any revalidation failure (no writes attempted), a plain error
    string on a malformed `dispositions` payload (existing store_memory error-string convention),
    or the same success-message string shape a normal store_memory call returns.

    Revalidation happens in two passes (Codex implementation-review finding #4): a cheap early
    pass outside any transaction (token shape/expiry/fingerprint -- fail fast on an obviously
    stale/malformed token without paying for a BEGIN IMMEDIATE), and the AUTHORITATIVE per-
    candidate content_hash/status/is_core revalidation inside `_write`, run fresh against `c`
    (the in-transaction connection) as the first thing that closure does. The early pass is an
    optimization, not the safety boundary -- a concurrent write landing between the early pass and
    BEGIN IMMEDIATE is still caught by the in-transaction pass, closing the TOCTOU window a
    pre-transaction-only check would leave open.
    """
    token = _decode_review_token(review_token or "")
    if token is None or token.get("v") != 1:
        return {
            "status": "REVIEW_STALE",
            "stale_candidate_ids": [],
            "message": "review_token is malformed or unrecognized -- call store_memory again "
            "without review_token to get a fresh preflight, then resubmit dispositions.",
        }

    now = datetime.now(UTC)
    try:
        expires_at = datetime.fromisoformat(token["expires_at"])
    except Exception:
        expires_at = now - timedelta(seconds=1)  # treat unparsable expiry as already expired

    early_stale_ids: list[str] = []
    if now > expires_at:
        early_stale_ids.append("__token__")
    if _compute_write_fingerprint(proposed) != token.get("proposed_write_fingerprint"):
        early_stale_ids.append("__proposed_write__")
    if proposed.get("resolved_entity_id") != token.get("resolved_entity_id"):
        early_stale_ids.append("__resolved_entity_id__")
    if early_stale_ids:
        return {
            "status": "REVIEW_STALE",
            "stale_candidate_ids": early_stale_ids,
            "message": "DB state changed since preflight (or the review token expired) -- call "
            "store_memory again without review_token to get a fresh preflight, then resubmit "
            "dispositions against the new candidate set.",
        }

    token_candidates = {c["candidate_id"]: c for c in token.get("candidates", [])}

    if not isinstance(dispositions, list):
        return "Error: dispositions must be a list of {candidate_id, disposition} objects."

    given = {}
    for item in dispositions:
        cid = item.get("candidate_id") if isinstance(item, dict) else None
        if not cid or cid not in token_candidates:
            return f"Error: dispositions references an unknown candidate_id: {item}"
        given[cid] = item.get("disposition")

    missing = set(token_candidates) - set(given)
    if missing:
        return f"Error: every flagged candidate needs an explicit disposition, missing: {sorted(missing)}"

    requested_consolidate_ids = [cid for cid, d in given.items() if d == "consolidate"]
    if len(requested_consolidate_ids) > MAX_CONSOLIDATION_REQUEST_SIZE:
        return (
            f"Error: REJECT_TOO_MANY_CONSOLIDATION_PARENTS - {len(requested_consolidate_ids)} "
            f"'consolidate' dispositions exceeds the cap of {MAX_CONSOLIDATION_REQUEST_SIZE}. "
            "Resubmit with fewer 'consolidate' dispositions, split across multiple store_memory calls."
        )

    # Precompute centroids for the REQUESTED consolidate targets before opening the write
    # transaction, mirroring bulk_commit_consolidation's own precompute-before-the-lock pattern
    # (cost-hoisting only -- the authoritative is_core-based disposition validation below still
    # happens fresh inside the transaction; a requested-consolidate target that turns out to be
    # core by commit time aborts the whole write there, this precompute is just not wasted in
    # the common case).
    precomputed = None
    if requested_consolidate_ids:
        precompute_target_ids = [
            token_candidates[cid]["target_entity_id"] for cid in requested_consolidate_ids
        ]
        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            precompute_target_ids, conn, db_path
        )
        precomputed = (centroids, unresolved, observed_state)

    result_holder: dict = {}

    def _write(c):  # noqa: C901, PLR0912
        # Authoritative, in-transaction revalidation (Codex implementation-review finding #4):
        # re-checked fresh against `c`, inside BEGIN IMMEDIATE, not before it -- this is what
        # actually closes the TOCTOU window, not the early pass above. Also revalidates is_core
        # for EVERY token candidate here (Codex round-2 implementation-review finding: is_core is
        # token state the caller's disposition choice was based on, same as content_hash/status --
        # a drift there must surface as REVIEW_STALE too, not only as an opaque disposition-
        # rejected error for whichever specific candidate happened to choose a now-disallowed
        # option; a candidate resolved "distinct" against a target that silently flipped to core
        # is just as stale a decision as one resolved "consolidate", even though "distinct" alone
        # would never trip the allowed-set check below).
        stale_ids = []
        fresh_is_core: dict[str, bool] = {}
        for cid, tc in token_candidates.items():
            row = c.execute(
                "SELECT content_hash, status, is_core FROM entities WHERE id = ?",
                (tc["target_entity_id"],),
            ).fetchone()
            if not row or row[0] != tc["target_content_hash"] or row[1] != tc["target_status"]:
                stale_ids.append(cid)
                continue
            is_core_now = bool(row[2])
            fresh_is_core[cid] = is_core_now
            if is_core_now != bool(tc.get("target_is_core")):
                stale_ids.append(cid)
        if stale_ids:
            raise _ReviewStale(stale_ids)

        consolidate_targets: list[str] = []
        supersede_targets: list[str] = []
        elaborate_targets: list[str] = []

        for cid, disposition in given.items():
            tc = token_candidates[cid]
            # Defense in depth (Codex round-1/round-2 fix): derive the allowed set from the SAME
            # fresh entities.is_core read just taken above (never the token's own unsigned copy) --
            # by this point it's already confirmed to match the token's target_is_core anyway
            # (the stale-check above would have aborted otherwise), so this is now belt-and-
            # suspenders on top of that, not the only line of defense.
            is_core_now = fresh_is_core[cid]
            allowed = _CORE_SAFE_DISPOSITIONS if is_core_now else _NON_CORE_DISPOSITIONS
            if disposition not in ("distinct", *allowed):
                raise _DispositionRejected(
                    f"disposition '{disposition}' is not permitted for candidate {cid} "
                    f"(allowed: {['distinct', *allowed]})."
                )
            if disposition == "consolidate":
                consolidate_targets.append(tc["target_entity_id"])
            elif disposition == "supersede":
                supersede_targets.append(tc["target_entity_id"])
            elif disposition == "elaborate":
                elaborate_targets.append(tc["target_entity_id"])
            # "distinct" contributes nothing

        was_existing = False
        if consolidate_targets:
            if precomputed is not None:
                centroids, unresolved, observed_state = precomputed
            else:
                centroids, unresolved, observed_state = get_fresh_entity_centroids(
                    consolidate_targets, c, db_path
                )
            res = commit_consolidation(
                parent_ids=consolidate_targets,
                title=proposed.get("title"),
                content=proposed.get("content"),
                tags=proposed.get("tags"),
                scope=proposed.get("scope") or "shared",
                weight=proposed.get("weight") or 1,
                is_core=proposed.get("is_core"),
                owner_id=proposed.get("owner_id"),
                context_id=proposed.get("context_id"),
                metadata=proposed.get("metadata"),
                memory_type=proposed.get("memory_type"),
                db_connection=c,
                _in_transaction=True,
                _precomputed_centroids=centroids,
                _precomputed_unresolved=unresolved,
                _precomputed_observed_state=observed_state,
            )
            if res.startswith("Error"):
                raise RuntimeError(f"Disposition commit aborted (all-or-nothing): {res}")
            output_entity_id = res.split("ID: ")[-1].strip()
            message = res
        else:
            from saltmdb.domain.services.memory_service import _store_raw_entity

            output_entity_id, was_existing = _store_raw_entity(c, proposed)
            message = f"Knowledge stored successfully with ID: {output_entity_id}"

        for target_id in supersede_targets:
            res = store_relation(
                source_id=output_entity_id,
                target_id=target_id,
                predicate="supersedes",
                owner_id=proposed.get("owner_id"),
                db_connection=c,
                _in_transaction=True,
            )
            if res.startswith("Error"):
                raise RuntimeError(f"Disposition commit aborted (all-or-nothing): {res}")

        for target_id in elaborate_targets:
            res = store_relation(
                source_id=output_entity_id,
                target_id=target_id,
                predicate="elaborates_on",
                owner_id=proposed.get("owner_id"),
                db_connection=c,
                _in_transaction=True,
            )
            if res.startswith("Error"):
                raise RuntimeError(f"Disposition commit aborted (all-or-nothing): {res}")

        # Corrected during implementation review (Codex round-1 implementation-review finding
        # #3): the reconciliation doc's degenerate all-A case ("the result degenerates to a plain
        # distinct store, original A case, unchanged" -- §1.3) means genuinely byte-identical,
        # including the tip-suffix nudge -- not merely "same persisted row." Applies whenever the
        # output entity came from the raw-insert path (no `consolidate` disposition in this
        # batch), matching store_memory's own no-candidates condition exactly (`not was_existing
        # and tags`); `commit_consolidation`'s own message never had a tip-suffix concept and
        # still doesn't here, consistent with every other commit_consolidation caller.
        if not consolidate_targets and not was_existing and proposed.get("tags"):
            message += (
                " [Tip: consider calling manage_relation to link this to related "
                "entities/concepts you just stored.]"
            )

        result_holder["message"] = message

    try:
        write_transaction_retrying(conn, _write)
    except _ReviewStale as e:
        return {
            "status": "REVIEW_STALE",
            "stale_candidate_ids": e.stale_ids,
            "message": "DB state changed since preflight -- call store_memory again without "
            "review_token to get a fresh preflight, then resubmit dispositions against the new "
            "candidate set.",
        }
    except _DispositionRejected as e:
        return f"Error: {e}"

    return result_holder["message"]
