"""Shared singleton state for the memory_service package.

Reached into directly by external callers by dotted path: daemon/server.py accesses
`memory_service._embed_pool` / `memory_service._search_pool` for shutdown/monitoring
wiring, and daemon/dispatch.py accesses `memory_service.RETRIEVAL_TEXT_UNSET` as a
sentinel default. Keep these definitions here as the single source of truth, and
always access them via qualified `_shared.<name>` lookups from other submodules,
never a name captured at import time.

In particular, scripts/benchmarking/build_diverse_test_db.py reassigns
`memory_service._embed_pool` at runtime (shutdown + fresh ThreadPoolExecutor) --
any internal consumer that captured `_embed_pool` via `from ._shared import
_embed_pool` at import time would silently diverge from that reassignment and use a
stale/shut-down pool. No internal code currently submits to the pool, so nothing
breaks today, but this is the rule any future consumer must follow.

The logger name is hardcoded (not `__name__`) so it stays exactly
"saltmdb.domain.services.memory_service" -- matching the pre-split flat module --
regardless of which submodule under this package a given log call physically lives in.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Provenance:
    agent_id: str | None = None
    context_id: str | None = None
    agent_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """What a search must match: tag, metadata, type, core, context and session filters."""

    # tags_filter and metadata_filter are caller-owned mutable containers: frozen is shallow, and hash() raises when either is set; never use as a cache key.
    context_id: str | None = None
    agent_session_id: str | None = None
    tags_filter: list | None = None
    tag_operator: Literal["AND", "OR"] = "AND"
    metadata_filter: dict | None = None
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"] | None = (
        None
    )
    is_core: bool | None = None


@dataclass(frozen=True, slots=True)
class SearchTuning:
    """Ranking and candidate-generation knobs; every default is the behaviour of a plain search."""

    cross_encoder_candidate_cap: int | None = None
    cross_encoder_text_cap_chars: int | None = None
    use_chunk_candidates: bool = False
    oversampling_multiplier: int | None = None
    candidate_window: int | None = None
    chunk_weight: float | None = None
    use_retrieval_text_candidates: bool = False
    retrieval_fts_weight: float | None = None
    retrieval_vector_weight: float | None = None
    disable_semantic: bool = False
    prefer_durable_types: bool = False
    demote_superseded: bool = False
    collapse_supersedes_families: bool = False


@dataclass(frozen=True, slots=True)
class CoreDeclaration:
    is_core: bool | None = None
    core_reason: str | None = None
    core_exit_condition: str | None = None
    core_review_after: str | None = None
    detail_memory_ids: list | None = None


@dataclass(frozen=True, slots=True)
class EntityAnnotations:
    metadata: dict | None = None
    memory_type: str | None = None


logger = logging.getLogger("saltmdb.domain.services.memory_service")
_embed_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saltmdb-embed")
_search_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="saltmdb-search")
RETRIEVAL_TEXT_UNSET = object()
