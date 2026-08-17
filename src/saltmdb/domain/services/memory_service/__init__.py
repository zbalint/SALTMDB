"""Domain service for memory storage, search, and lifecycle.

Split from a single 3,630-line memory_service.py into this package along its natural
functional seams (see the refactor plan at
/home/zbalint/.claude/plans/read-the-handover-than-sparkling-liskov.md for the full
rationale and staged history). See the submodules for internal organization:

- `_shared`: singleton state reached into by external callers (logger, thread pools,
  RETRIEVAL_TEXT_UNSET).
- `validation`: input validation and retrieval-text helpers.
- `tags`: tag normalization and canonical-tag resolution.
- `write`: store_memory and its raw-entity persistence path.
- `search_primitives`: FTS/vector/chunk candidate lookups and RRF fusion.
- `ranking`: ranking and supersession post-processing.
- `orchestrator`: the search_memory orchestrator itself.
- `lifecycle`: fetch/touch/archive/orphan-scan.
- `duplicates`: duplicate detection, memory scanning, bulk archive.

This module re-exports the full public+legacy-private surface so existing
`from saltmdb.domain.services.memory_service import X` and `memory_service.X` call
sites (disposition_service.py, librarian_service.py, relation_service.py,
daemon/server.py, daemon/dispatch.py, viewer/routes.py, benchmarking scripts, and the
test suite) are unaffected by the internal split. This deliberately deviates from this
repo's other multi-module package (`saltmdb.utils`, which uses direct-submodule
imports and a bare-docstring `__init__.py`) -- justified because this package's
external blast radius (including private-name and mock.patch dotted-path reach-ins)
is categorically larger.
"""

from ._shared import logger, _embed_pool, _search_pool, RETRIEVAL_TEXT_UNSET
from .validation import (
    normalize_retrieval_text,
    get_last_search_diagnostics,
    validate_memory_input,
    TITLE_MIN_LENGTH,
    TITLE_MAX_LENGTH,
    _retrieval_text_hash,
    _set_search_diagnostics,
    _validate_chunk_candidate_controls,
    _validate_retrieval_text_controls,
    _validate_cross_encoder_controls,
)
from .tags import (
    normalize_tag_name,
    resolve_or_create_tag,
    get_canonical_tags,
    _TAG_NAME_RE,
)
from .write import store_memory, _resolve_existing_entity_id, _store_raw_entity
from .search_primitives import (
    STOP_WORDS,
    semantic_search,
    chunk_candidate_search,
    retrieval_vector_search,
    rerank_candidates_by_topic,
    reciprocal_rank_fusion,
    weighted_reciprocal_rank_fusion,
    _run_fts_search,
    _run_retrieval_fts_search,
    _batch_semantic_similarities,
    _score_topics_with_fallback,
)
from .ranking import (
    accept_or_abstain,
    _rrf_gap_confident,
    _apply_type_bias,
    _compute_superseded_ids,
    _compute_bitemporal_target_ids,
    _compute_superseded_ids_bitemporal,
    _apply_supersession_demotion,
    _collapse_supersedes_families,
    _build_cross_encoder_candidate_texts,
    _apply_strict_ranking_defaults,
    _resolve_supersession_chains,
    _substitute_resolved_heads,
    _build_candidate_evidence,
)
from .orchestrator import search_memory
from .lifecycle import (
    fetch_memory_chunk,
    touch_memory_access,
    archive_memory,
    detect_orphaned_memories,
)
from .duplicates import check_duplicate_memories, scan_memories, bulk_archive_memory

__all__ = [
    "logger",
    "_embed_pool",
    "_search_pool",
    "RETRIEVAL_TEXT_UNSET",
    "normalize_retrieval_text",
    "get_last_search_diagnostics",
    "validate_memory_input",
    "TITLE_MIN_LENGTH",
    "TITLE_MAX_LENGTH",
    "_retrieval_text_hash",
    "_set_search_diagnostics",
    "_validate_chunk_candidate_controls",
    "_validate_retrieval_text_controls",
    "_validate_cross_encoder_controls",
    "normalize_tag_name",
    "resolve_or_create_tag",
    "get_canonical_tags",
    "_TAG_NAME_RE",
    "store_memory",
    "_resolve_existing_entity_id",
    "_store_raw_entity",
    "STOP_WORDS",
    "semantic_search",
    "chunk_candidate_search",
    "retrieval_vector_search",
    "rerank_candidates_by_topic",
    "reciprocal_rank_fusion",
    "weighted_reciprocal_rank_fusion",
    "_run_fts_search",
    "_run_retrieval_fts_search",
    "_batch_semantic_similarities",
    "_score_topics_with_fallback",
    "accept_or_abstain",
    "_rrf_gap_confident",
    "_apply_type_bias",
    "_compute_superseded_ids",
    "_compute_bitemporal_target_ids",
    "_compute_superseded_ids_bitemporal",
    "_apply_supersession_demotion",
    "_collapse_supersedes_families",
    "_build_cross_encoder_candidate_texts",
    "_apply_strict_ranking_defaults",
    "_resolve_supersession_chains",
    "_substitute_resolved_heads",
    "_build_candidate_evidence",
    "search_memory",
    "fetch_memory_chunk",
    "touch_memory_access",
    "archive_memory",
    "detect_orphaned_memories",
    "check_duplicate_memories",
    "scan_memories",
    "bulk_archive_memory",
]
