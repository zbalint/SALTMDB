"""Input validation and retrieval-text helpers for memory_service.

Split out of the former flat memory_service.py (see plan at
/home/zbalint/.claude/plans/read-the-handover-than-sparkling-liskov.md) as a pure
code-motion extraction -- no behavior changes. Leaf helpers with zero cross-module
dependencies within this package.
"""

import hashlib
import threading
import unicodedata
from typing import Any

from saltmdb.config import (
    RETRIEVAL_TEXT_MAX_CHARS,
    RETRIEVAL_TEXT_DEFAULT_FTS_WEIGHT,
    RETRIEVAL_TEXT_DEFAULT_VECTOR_WEIGHT,
    RETRIEVAL_TEXT_RRF_WEIGHT_OPTIONS,
)
from saltmdb.utils.redaction import redact_secrets

_search_diagnostics = threading.local()

TITLE_MIN_LENGTH = 5
TITLE_MAX_LENGTH = 200


def _retrieval_text_hash(value: str | None) -> str | None:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest() if value else None


def normalize_retrieval_text(value: str) -> str | None:
    """Normalize/redact optional retrieval text, returning ``None`` for an explicit clear.

    The authoritative memory body is never passed through this helper.  NFKC makes equivalent
    Unicode spellings comparable for FTS/vector jobs, surrounding whitespace is removed, and the
    post-redaction cap is measured in Python Unicode code points (not UTF-8 bytes).
    """
    if not isinstance(value, str):
        raise ValueError("retrieval_text must be a string; JSON null is ambiguous and rejected")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return None
    redacted = redact_secrets(normalized)
    if len(redacted) > RETRIEVAL_TEXT_MAX_CHARS:
        raise ValueError(
            f"retrieval_text exceeds the maximum length of {RETRIEVAL_TEXT_MAX_CHARS} Unicode characters"
        )
    return redacted


def _set_search_diagnostics(value: dict[str, Any]) -> None:
    """Publish diagnostics for the current search thread without changing legacy result shapes.

    The daemon serves searches concurrently, so a module-global ``last`` value would allow one
    request to overwrite another request's benchmark evidence.  Thread-local storage keeps the
    lightweight diagnostics available to direct benchmark callers while ``return_diagnostics``
    provides an explicit transport-safe envelope for MCP/RPC callers that request it.
    """
    _search_diagnostics.value = dict(value)


def get_last_search_diagnostics() -> dict[str, Any]:
    """Return a copy of the most recent search diagnostics for this worker thread."""
    return dict(getattr(_search_diagnostics, "value", {}))


def _validate_chunk_candidate_controls(
    use_chunk_candidates: bool,
    oversampling_multiplier: int | None,
    candidate_window: int | None,
    chunk_weight: float | None,
) -> tuple[int, int, float]:
    """Validate and normalize the bounded chunk-candidate experiment controls.

    Stage-1's frozen broad baseline intentionally carries sentinel values (1/0/0.0) while the
    feature is disabled.  Those sentinels remain accepted and ignored in that mode so adding the
    fields to a benchmark manifest cannot alter ordinary search.  Once enabled, only the signed
    experiment values are accepted.
    """
    from saltmdb.config import (
        CHUNK_CANDIDATE_DEFAULT_OVERSAMPLING,
        CHUNK_CANDIDATE_DEFAULT_WINDOW,
        CHUNK_CANDIDATE_OVERSAMPLING_OPTIONS,
        CHUNK_CANDIDATE_WINDOW_OPTIONS,
        CHUNK_RRF_WEIGHT_OPTIONS,
    )

    if not use_chunk_candidates:
        return (
            CHUNK_CANDIDATE_DEFAULT_OVERSAMPLING,
            CHUNK_CANDIDATE_DEFAULT_WINDOW,
            CHUNK_RRF_WEIGHT_OPTIONS[1],
        )

    oversampling = (
        CHUNK_CANDIDATE_DEFAULT_OVERSAMPLING
        if oversampling_multiplier is None
        else oversampling_multiplier
    )
    window = CHUNK_CANDIDATE_DEFAULT_WINDOW if candidate_window is None else candidate_window
    weight = CHUNK_RRF_WEIGHT_OPTIONS[1] if chunk_weight is None else chunk_weight
    if isinstance(oversampling, bool) or not isinstance(oversampling, int):
        raise ValueError("oversampling_multiplier must be one of 4, 8, or 12")
    if oversampling not in CHUNK_CANDIDATE_OVERSAMPLING_OPTIONS:
        raise ValueError("oversampling_multiplier must be one of 4, 8, or 12")
    if isinstance(window, bool) or not isinstance(window, int):
        raise ValueError("candidate_window must be one of 20, 40, or 60")
    if window not in CHUNK_CANDIDATE_WINDOW_OPTIONS:
        raise ValueError("candidate_window must be one of 20, 40, or 60")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise ValueError("chunk_weight must be one of 0.5, 1.0, or 1.5")
    if float(weight) not in CHUNK_RRF_WEIGHT_OPTIONS:
        raise ValueError("chunk_weight must be one of 0.5, 1.0, or 1.5")
    return oversampling, window, float(weight)


def _validate_retrieval_text_controls(
    enabled: bool,
    retrieval_fts_weight: float | None,
    retrieval_vector_weight: float | None,
) -> tuple[float, float]:
    """Validate independent retrieval-text channel weights without changing disabled defaults."""
    if not enabled:
        return 0.0, 0.0
    fts_weight = (
        RETRIEVAL_TEXT_DEFAULT_FTS_WEIGHT if retrieval_fts_weight is None else retrieval_fts_weight
    )
    vector_weight = (
        RETRIEVAL_TEXT_DEFAULT_VECTOR_WEIGHT
        if retrieval_vector_weight is None
        else retrieval_vector_weight
    )
    for name, value in (
        ("retrieval_fts_weight", fts_weight),
        ("retrieval_vector_weight", vector_weight),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be one of 0.5, 1.0, or 1.5")
        if float(value) not in RETRIEVAL_TEXT_RRF_WEIGHT_OPTIONS:
            raise ValueError(f"{name} must be one of 0.5, 1.0, or 1.5")
    return float(fts_weight), float(vector_weight)


def _validate_cross_encoder_controls(
    candidate_cap: int | None, text_cap_chars: int | None, *, enabled: bool = True
) -> tuple[int, int]:
    """Validate bounded cross-encoder controls and return config defaults for omitted values."""
    from saltmdb.config import (
        CROSS_ENCODER_CANDIDATE_CAP_OPTIONS,
        CROSS_ENCODER_MAX_CHARS,
        CROSS_ENCODER_MAX_CANDIDATES,
        CROSS_ENCODER_TEXT_CAP_OPTIONS,
    )

    cap = CROSS_ENCODER_MAX_CANDIDATES if candidate_cap is None else candidate_cap
    text_cap = CROSS_ENCODER_MAX_CHARS if text_cap_chars is None else text_cap_chars
    if not enabled:
        # Optional controls are inert when CE itself is disabled.  This preserves ordinary search
        # compatibility for callers carrying experiment metadata alongside the legacy path.
        return CROSS_ENCODER_MAX_CANDIDATES, CROSS_ENCODER_MAX_CHARS
    if (
        isinstance(cap, bool)
        or not isinstance(cap, int)
        or cap not in CROSS_ENCODER_CANDIDATE_CAP_OPTIONS
    ):
        raise ValueError("cross_encoder_candidate_cap must be one of 10, 15, or 20")
    if (
        isinstance(text_cap, bool)
        or not isinstance(text_cap, int)
        or text_cap not in CROSS_ENCODER_TEXT_CAP_OPTIONS
    ):
        raise ValueError("cross_encoder_text_cap_chars must be one of 1000 or 2000")
    return cap, text_cap


def validate_memory_input(title: str, content: str, metadata: dict | None) -> None:
    """Validates memory input to enforce title length bounds."""
    if title:
        stripped_title = title.strip()
        if len(stripped_title) > TITLE_MAX_LENGTH:
            raise ValueError(
                f"Error: Title exceeds the maximum length of {TITLE_MAX_LENGTH} characters (got {len(stripped_title)}). "
                "Titles must be a short canonical label in '[Domain] Topic' form, not the memory body. "
                "Move the full text into the 'content' parameter."
            )
        if len(stripped_title) < TITLE_MIN_LENGTH:
            raise ValueError(
                f"Error: Title is too short (minimum {TITLE_MIN_LENGTH} characters). Provide a descriptive, canonical title."
            )
