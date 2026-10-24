"""Token-budget packing for graph-aware context retrieval."""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from saltmdb.config import (
    CONTEXT_BUDGET_DEFAULT_TOKENS,
    CONTEXT_BUDGET_MAX_TOKENS,
    get_db_path,
)
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.embedding_service import get_model

logger = logging.getLogger(__name__)


def _empty_budget_result(effective_budget: int) -> dict[str, Any]:
    return {
        "packed_entity_ids": {"primary": [], "expansion": [], "community_member": []},
        "dropped_entity_ids": {"primary": [], "expansion": [], "community_member": []},
        "conflict_only_entity_ids": [],
        "community_representative_entity_ids": [],
        "token_counts": {},
        "budget": {
            "unit": "tokens",
            "limit": effective_budget,
            "used": 0,
            "primary_truncated": False,
            "primary_dropped_count": 0,
            "expansion_truncated": False,
            "expansion_dropped_count": 0,
            "conflict_reserve_tokens_used": 0,
            "orphan_community_reserve_tokens_used": 0,
            "community_member_truncated": False,
            "community_member_dropped_count": 0,
            "community_representative_reserve_tokens_used": 0,
        },
    }


@dataclass(frozen=True, slots=True)
class _Section:
    """What one packing pass kept and what it dropped, in input order."""

    # not hashable: carries a list/set
    packed: list[str]
    dropped: list[str]


@dataclass(frozen=True, slots=True)
class _ReserveIds:
    """Ids charged to a reserve outside the ordinary budget."""

    # not hashable: carries a list/set
    conflict: set[str]
    orphan: set[str]
    representative: set[str]


@dataclass(slots=True)
class _BudgetLedger:
    """Token costs, the limit and the running total of one packing pass."""

    # mutable accumulator; carries a dict
    token_counts: dict[str, int]
    limit: int
    used: int = 0

    def pack(self, ids: list[str]) -> _Section:
        packed: list[str] = []
        dropped: list[str] = []
        for entity_id in ids:
            cost = self.token_counts[entity_id]
            if self.used + cost <= self.limit:
                packed.append(entity_id)
                self.used += cost
            else:
                dropped.append(entity_id)
        return _Section(packed, dropped)


def _load_token_counts(conn: sqlite3.Connection, all_ids: set[str]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in all_ids)
    rows = conn.execute(
        f"SELECT id, full_content FROM entities WHERE id IN ({placeholders})", tuple(all_ids)
    ).fetchall()
    content_by_id = {row[0]: row[1] for row in rows}
    model = get_model()
    return {
        entity_id: 0
        if content_by_id.get(entity_id, "") == ""
        else model.token_count(content_by_id.get(entity_id, ""))
        for entity_id in all_ids
    }


def _budget_output(
    ledger: _BudgetLedger,
    primary: _Section,
    expansion: _Section,
    members: _Section,
    reserves: _ReserveIds,
) -> dict[str, Any]:
    return {
        "packed_entity_ids": {
            "primary": primary.packed,
            "expansion": expansion.packed,
            "community_member": members.packed,
        },
        "dropped_entity_ids": {
            "primary": primary.dropped,
            "expansion": expansion.dropped,
            "community_member": members.dropped,
        },
        "conflict_only_entity_ids": sorted(reserves.conflict),
        "community_representative_entity_ids": sorted(reserves.representative),
        "token_counts": ledger.token_counts,
        "budget": {
            "unit": "tokens",
            "limit": ledger.limit,
            "used": ledger.used,
            "primary_truncated": bool(primary.dropped),
            "primary_dropped_count": len(primary.dropped),
            "expansion_truncated": bool(expansion.dropped),
            "expansion_dropped_count": len(expansion.dropped),
            "conflict_reserve_tokens_used": sum(ledger.token_counts[i] for i in reserves.conflict),
            "orphan_community_reserve_tokens_used": sum(
                ledger.token_counts[i] for i in reserves.orphan
            ),
            "community_member_truncated": bool(members.dropped),
            "community_member_dropped_count": len(members.dropped),
            "community_representative_reserve_tokens_used": sum(
                ledger.token_counts[i] for i in reserves.representative
            ),
        },
    }


def _open_budget_connection(
    db_connection: sqlite3.Connection | None, db_path: str | None
) -> tuple[sqlite3.Connection, bool]:
    if db_connection:
        return db_connection, False
    return get_connection(db_path or get_db_path()), True


def pack_context_budget(
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    conflict_sets_result: dict[str, Any],
    *,
    budget_tokens: int | None = None,
    community_member_ids: list[str] | None = None,
    community_representative_ids: set[str] | None = None,
    orphan_community_entity_ids: set[str] | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Pack primary and expansion candidates against a real-token budget."""
    effective_budget = min(
        budget_tokens if budget_tokens is not None else CONTEXT_BUDGET_DEFAULT_TOKENS,
        CONTEXT_BUDGET_MAX_TOKENS,
    )
    primary_ids = [hit["id"] for hit in primary_hits]
    expansion_ids = [
        candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
    ]
    conflict_only_ids = {
        member["id"]
        for conflict_set in conflict_sets_result["conflict_sets"]
        for member in conflict_set["members"]
        if member["inclusion"] == "conflict_only"
    }
    orphan_ids = orphan_community_entity_ids or set()
    member_ids = community_member_ids or []
    representative_ids = community_representative_ids or set()
    if not any(
        (primary_ids, expansion_ids, conflict_only_ids, orphan_ids, member_ids, representative_ids)
    ):
        return _empty_budget_result(effective_budget)
    conn, should_close = _open_budget_connection(db_connection, db_path)
    try:
        all_ids = (
            set(primary_ids)
            | set(expansion_ids)
            | conflict_only_ids
            | orphan_ids
            | set(member_ids)
            | representative_ids
        )
        token_counts = _load_token_counts(conn, all_ids)
        ledger = _BudgetLedger(token_counts, effective_budget)
        primary = ledger.pack(primary_ids)
        expansion = ledger.pack(expansion_ids)
        members = ledger.pack(member_ids)
        reserves = _ReserveIds(conflict_only_ids, orphan_ids, representative_ids)
        return _budget_output(ledger, primary, expansion, members, reserves)
    finally:
        if should_close:
            close_connection(conn)
