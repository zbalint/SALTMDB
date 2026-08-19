"""Closed relation-predicate vocabulary (agent API redesign plan §5.8, Phase 6 item 25).

Pure Python, zero DB access -- safe to import from both the MCP adapter process
(mcp/tools.py, matching the existing exception for saltmdb.utils.corrected_call/envelope and
core_governance_service.parse_is_core) and the domain layer (relation_service.py), which is
what lets both layers enforce the same closed enum without either importing the other.

Mirrors plan §7.1's authoritative migration mapping exactly -- one list, two consumers (this
module gates Phase 6 writes; §7.1's SQL statements migrate Phase 8's historical rows) -- so the
write-time gate and the data migration can never drift apart.

Three closed categories plus one open alias table:

- **Agent-selectable (11):** the canonical predicates ``manage_relation`` accepts directly.
- **Reserved (3):** ``supersedes``/``consolidated_from``/``revises`` -- system-owned, created
  only by their matching lifecycle tool. Forging one would corrupt a system invariant (demote a
  memory in ranking without archiving it, or fabricate lineage ``get_lineage`` reports as fact),
  which is the line the plan draws between "reserved" and merely "system-created" (§5.8's
  reservation principle).
- **Legacy, read-only (1):** ``similar_to``. Its existing edges stay readable/traversable
  forever (§1.4, information is never lost); no new ones may be created, by agents or the
  system, since the mechanical cosine-similarity auto-linker that used to create it was retired
  before this redesign (AGENT_GUIDE.md:128, "don't use it yourself").
- **Aliases (36):** drifted spellings from live-DB measurement (memory ``18575b72``), each
  mapped to its canonical replacement. 30 are same-direction renames; 6 also require swapping
  ``source_id``/``target_id`` because the drifted verb read the relationship from the opposite
  end (e.g. ``A resolved_by B`` means ``B resolves A``).

Every one of these 51 names (11 + 3 + 1 + 36) is accounted for exactly once -- no name appears
in two categories -- which is what makes ``classify_predicate`` exhaustive rather than a
best-effort heuristic.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Agent-selectable canonical predicates (11). `contradicts` is new (zero live edges at plan
# time) but already has gate support -- RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS predates the
# predicate's own existence in the selectable set.
AGENT_SELECTABLE_PREDICATES: frozenset[str] = frozenset(
    {
        "elaborates_on",
        "related_to",
        "resolves",
        "depends_on",
        "verifies",
        "corrects",
        "caused_by",
        "derived_from",
        "distinguishes_from",
        "part_of",
        "contradicts",
    }
)

# Reserved (system-owned) predicates -> the lifecycle tool that is the only legitimate creator.
# manage_relation refuses all three and names the correct tool instead.
RESERVED_PREDICATES: dict[str, str] = {
    "supersedes": "supersede_memory",
    "consolidated_from": "consolidate_memories",
    "revises": "revise_memory",
}

# Legacy, read-only: existing edges remain traversable; no new writes, by agents or the system.
LEGACY_READONLY_PREDICATES: frozenset[str] = frozenset({"similar_to"})

# Drifted spelling -> (canonical name, swap_source_and_target). Authoritative mapping from plan
# §7.1 -- 30 same-direction renames plus 6 direction-swaps, exactly matching the Phase 8 SQL
# migration's WHERE clauses so a predicate rejected today migrates identically tomorrow.
PREDICATE_ALIASES: dict[str, tuple[str, bool]] = {
    # -> related_to (same direction)
    "relates_to": ("related_to", False),
    "references": ("related_to", False),
    "links_to": ("related_to", False),
    "routes_to": ("related_to", False),
    "applies_to": ("related_to", False),
    "defines_protocol": ("related_to", False),
    "discovered_during": ("related_to", False),
    "enhances_retrieval": ("related_to", False),
    "governs_routing": ("related_to", False),
    "invokes_subagent": ("related_to", False),
    # -> resolves
    "fixes_bug_in": ("resolves", False),
    "fulfills": ("resolves", False),
    "fixes": ("resolves", False),
    "resolves_issue_in": ("resolves", False),
    "resolved_by": ("resolves", True),  # A resolved_by B -> B resolves A
    "remediated_by": ("resolves", True),  # same shape
    # -> verifies
    "confirms": ("verifies", False),
    "reviews": ("verifies", False),
    "tests_tool_for": ("verifies", False),
    "verified_by": ("verifies", True),  # A verified_by B -> B verifies A
    # -> derived_from
    "extends": ("derived_from", False),
    "inspired_by": ("derived_from", False),
    "continues": ("derived_from", False),
    "follows": ("derived_from", False),
    "implements": ("derived_from", False),
    # -> elaborates_on
    "elaborates": ("elaborates_on", False),
    "documents": ("elaborates_on", False),
    "documents_history_for": ("elaborates_on", False),
    "summarizes": ("elaborates_on", True),  # A summarizes B -> B elaborates_on A
    "expanded_by": ("elaborates_on", True),  # A expanded_by B -> B elaborates_on A
    # -> corrects
    "amends": ("corrects", False),
    "refines": ("corrects", False),
    # -> caused_by
    "affected_by": ("caused_by", False),  # rename only: A affected_by B and A caused_by B
    # both already mean "B acted on A" -- not a swap.
    "affects": ("caused_by", True),  # A affects B -> B caused_by A
    # -> depends_on
    "has_constraint": ("depends_on", False),
    "uses_architecture": ("depends_on", False),
}


def normalize_predicate_name(raw: str | None) -> str:
    """Shape-normalizes a predicate string (lowercase, non-alnum runs -> underscore, trimmed).
    Mirrors relation_service._normalize_predicate_name -- kept here too (not imported from
    there) so this module has zero dependency on the domain layer, only the reverse."""
    return re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")


class PredicateDisposition(NamedTuple):
    """Result of classifying one submitted predicate string against the closed vocabulary.

    status: "selectable" (already canonical, store as-is), "alias" (drifted spelling, reject
    with a corrected_call), "reserved" (system-owned, reject naming the lifecycle tool),
    "legacy_readonly" (similar_to, reject -- read-only), or "unknown" (not in the closed
    universe of 51 names at all, reject with no derivable correction).
    canonical: the canonical predicate name, when known (selectable/alias/reserved/
    legacy_readonly); None for "unknown".
    swap: True when a corrected call must exchange source_id/target_id (only ever True for
    "alias").
    lifecycle_tool: the tool name that legitimately creates this predicate; only set for
    "reserved".
    """

    status: str
    canonical: str | None
    swap: bool
    lifecycle_tool: str | None


def classify_predicate(raw: str | None) -> PredicateDisposition:
    """Classifies a raw, agent-submitted predicate string against the closed vocabulary.

    Never raises and never touches the database -- pure lookup against the module-level
    tables above. Callers (mcp/tools.py's manage_relation pre-flight, relation_service.py's
    store_relation write gate) decide what to do with a non-"selectable" disposition.
    """
    normalized = normalize_predicate_name(raw)
    if not normalized:
        return PredicateDisposition("unknown", None, False, None)
    if normalized in AGENT_SELECTABLE_PREDICATES:
        return PredicateDisposition("selectable", normalized, False, None)
    if normalized in RESERVED_PREDICATES:
        return PredicateDisposition("reserved", normalized, False, RESERVED_PREDICATES[normalized])
    if normalized in LEGACY_READONLY_PREDICATES:
        return PredicateDisposition("legacy_readonly", normalized, False, None)
    if normalized in PREDICATE_ALIASES:
        canonical, swap = PREDICATE_ALIASES[normalized]
        return PredicateDisposition("alias", canonical, swap, None)
    return PredicateDisposition("unknown", None, False, None)
