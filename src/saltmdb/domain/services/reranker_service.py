"""Optional ONNX cross-encoder Stage-2 reranker (roadmap `ba2cf66f` P1#7, design memos `1fddc04a`/
`8115fa4a`). Lazy, feature-flagged (`SALTMDB_RERANKER_MODEL`, default unset/disabled), no PyTorch
runtime -- `fastembed` (already pinned, already used for the bi-encoder) wraps ONNX Runtime for its
`TextCrossEncoder` API too, confirmed live during planning (SALTMDB event `345bdd37`).

Mirrors `embedding_service.py`'s free-function, lazy-singleton-with-thread-lock style rather than a
class-based singleton, matching this codebase's existing convention. Only the benchmark-selected
winning candidate (`Xenova/ms-marco-MiniLM-L-6-v2`, ~88MB -- the smallest of the three benchmarked
candidates, and the only one under the project's 100MB bundling budget; see SALTMDB event
`958fdb99` for the full benchmark writeup) is bundled locally under `src/saltmdb/models/`, same
bundled-local-dir-with-online-fallback convention `embedding_service.py`'s bi-encoder already uses.
Every other supported model name in `CROSS_ENCODER_SUPPORTED_MODELS` (0.12GB-1.11GB, all well over
budget) is online-load-only.
"""

import logging
import math
import os
import threading
from typing import Protocol

logger = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model = None
_model_name = None  # tracks which name _model was loaded for; a changed env var mid-process
# (test isolation, not a real prod scenario) forces a fresh load rather than silently reusing a
# stale singleton for the wrong model name.

# The one bundled candidate (see module docstring). Layout note: unlike embedding_service.py's
# bi-encoder bundle (a flat directory containing model_optimized.onnx directly),
# fastembed.rerank.cross_encoder.TextCrossEncoder resolves a local model through
# huggingface_hub's OWN standard cache layout (`models--<org>--<repo>/{refs,snapshots}` under
# `cache_dir`) -- verified empirically during implementation, TextEmbedding's flat-bundle
# convention does not apply to TextCrossEncoder. `_BUNDLED_MODEL_CACHE_DIR` below is therefore the
# `cache_dir` PARENT to pass to TextCrossEncoder, not the model directory itself.
_BUNDLED_MODEL_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"
_BUNDLED_MODEL_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "models", "ms-marco-MiniLM-L-6-v2")
)
_BUNDLED_MODEL_ONNX_PATH = os.path.join(
    _BUNDLED_MODEL_CACHE_DIR,
    "models--Xenova--ms-marco-MiniLM-L-6-v2",
    "snapshots",
    "a09144355adeed5f58c8ed011d209bf8ee5a1fec",
    "onnx",
    "model.onnx",
)


class Reranker(Protocol):
    """Structural contract this module's free functions satisfy (SALTMDB roadmap `ba2cf66f`
    P1#7, design memo `1fddc04a`'s "define a lazy Reranker protocol"). Documents the interface a
    future object-oriented or alternative-backend implementation would need to match; this
    module's own `score_pairs` free function is what every current caller actually uses.
    """

    def score_pairs(self, query: str, candidates: list[str]) -> list[float] | None: ...


def _is_valid_bundled_model() -> bool:
    """Verify the bundled ONNX file exists and isn't a truncated/un-fetched placeholder --
    mirrors embedding_service.py's _is_valid_local_model size-sanity check (guards against a
    Git LFS pointer or an interrupted checkout, not just a missing file)."""
    if not os.path.isfile(_BUNDLED_MODEL_ONNX_PATH):
        return False
    try:
        if os.path.getsize(_BUNDLED_MODEL_ONNX_PATH) < 10 * 1024 * 1024:
            logger.warning(
                "Bundled reranker model file %s is too small (likely an un-fetched Git LFS "
                "pointer). Skipping local load.",
                _BUNDLED_MODEL_ONNX_PATH,
            )
            return False
    except OSError:
        return False
    return True


def get_reranker_model_name() -> str | None:
    """SALTMDB_RERANKER_MODEL env var, stripped; None/empty -> disabled (default)."""
    from saltmdb.config import get_reranker_model_name as _cfg

    return _cfg()


def is_cross_encoder_enabled() -> bool:
    """False when unset/empty (default, silent -- this is the expected common case, not worth a
    log line). An unsupported name IS logged: a misconfigured env var is a real setup mistake
    worth surfacing, unlike simply not opting in. Logs at WARNING every call it's misconfigured
    (matches this codebase's existing precedent, e.g. rerank_candidates_by_topic's own
    un-rate-limited failure warning -- no new rate-limiting mechanism introduced here).
    """
    name = get_reranker_model_name()
    if not name:
        return False
    from saltmdb.config import CROSS_ENCODER_SUPPORTED_MODELS

    if name not in CROSS_ENCODER_SUPPORTED_MODELS:
        logger.warning(
            "SALTMDB_RERANKER_MODEL=%r is not in CROSS_ENCODER_SUPPORTED_MODELS; cross-encoder "
            "reranking disabled.",
            name,
        )
        return False
    return True


def get_model(model_name: str):
    """Lazy singleton, mirrors embedding_service.get_model()'s thread-lock shape: try the bundled
    local copy first for the one bundled model name, falling back to an online load on any
    failure (missing/invalid bundle, or any other model name entirely).
    """
    global _model, _model_name
    if _model is None or _model_name != model_name:
        with _model_lock:
            if _model is None or _model_name != model_name:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                if model_name == _BUNDLED_MODEL_NAME and _is_valid_bundled_model():
                    logger.info(
                        "Loading bundled ONNX cross-encoder model from %s",
                        _BUNDLED_MODEL_CACHE_DIR,
                    )
                    try:
                        _model = TextCrossEncoder(
                            model_name=model_name,
                            cache_dir=_BUNDLED_MODEL_CACHE_DIR,
                            local_files_only=True,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to load bundled reranker model from %s: %s. Falling back to "
                            "online model load.",
                            _BUNDLED_MODEL_CACHE_DIR,
                            e,
                        )
                        _model = TextCrossEncoder(model_name=model_name)
                else:
                    _model = TextCrossEncoder(model_name=model_name)
                _model_name = model_name
    return _model


def score_pairs(query: str, candidates: list[str]) -> list[float] | None:
    """Returns per-candidate raw cross-encoder logits, index-aligned with `candidates`, or None
    on ANY of: disabled (no/unsupported SALTMDB_RERANKER_MODEL), empty candidates, a runner
    exception (model load failure, OOM, malformed input), or a malformed/untrustworthy model
    output -- caller MUST treat None as "no signal, fall back to whatever ordering/evidence would
    exist without this stage," never as all-zero scores (a zero score is a real, meaningful
    low-relevance signal; None is "this stage didn't run at all").

    Truncates each candidate to CROSS_ENCODER_MAX_CHARS chars AND the query to
    CROSS_ENCODER_MAX_QUERY_CHARS chars before scoring -- an uncapped query would be concatenated
    into EVERY pair the model scores, so its cost multiplies across the whole capped candidate
    list, not just once. These caps are distinct from CHUNK_SIZE_CHARS -- cross-encoders take a
    single concatenated query+candidate sequence through one forward pass per pair, so cost scales
    directly with combined length in a way the bi-encoder's independently-embedded, precomputed
    chunks don't. Caps `candidates` to CROSS_ENCODER_MAX_CANDIDATES -- overflow candidates are NOT
    scored (the caller is responsible for deciding what happens to them, e.g. appending them
    unscored at the tail; this function never silently drops caller-visible items, it just doesn't
    score all of them).
    """
    if not candidates or not is_cross_encoder_enabled():
        return None

    from saltmdb.config import (
        CROSS_ENCODER_MAX_CANDIDATES,
        CROSS_ENCODER_MAX_CHARS,
        CROSS_ENCODER_MAX_QUERY_CHARS,
    )

    model_name = get_reranker_model_name()
    if model_name is None:
        return None
    capped_query = (query or "")[:CROSS_ENCODER_MAX_QUERY_CHARS]
    capped = [c[:CROSS_ENCODER_MAX_CHARS] for c in candidates[:CROSS_ENCODER_MAX_CANDIDATES]]
    try:
        model = get_model(model_name)
        scores = list(model.rerank(capped_query, capped))
    except Exception as e:
        logger.warning("Cross-encoder reranking failed (model=%s): %s", model_name, e)
        return None

    # A short/long/non-numeric/NaN/bool output would otherwise silently produce a partial zip()
    # map (mislabeling scores against the wrong candidate) or a garbage-but-truthy ordering
    # instead of the promised deterministic None fallback -- validate cardinality AND per-value
    # finiteness before trusting the output at all. `bool` is explicitly excluded even though
    # Python's `isinstance(True, int)` is True -- a boolean is never a legitimate cross-encoder
    # logit, and letting it silently pass as 0.0/1.0 would violate the `list[float]` contract.
    if len(scores) != len(capped) or not all(
        isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) for s in scores
    ):
        logger.warning(
            "Cross-encoder reranking returned malformed output (model=%s, expected %d scores, "
            "got %r); falling back.",
            model_name,
            len(capped),
            scores,
        )
        return None
    return scores
