"""Leakage-safe ranking policies for the search architecture bakeoff.

The code in this module is evaluation-only.  It consumes frozen retrieval features and never
opens the SALTMDB corpus, which makes family-grouped cross-validation and feature audits easy to
verify independently of the production search service.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence


CROSS_ENCODER_MODELS = (
    "Xenova/ms-marco-MiniLM-L-6-v2",
    "Xenova/ms-marco-MiniLM-L-12-v2",
    "jinaai/jina-reranker-v1-tiny-en",
    "jinaai/jina-reranker-v1-turbo-en",
    "BAAI/bge-reranker-base",
)
CROSS_ENCODER_TEXT_VERSION = "query-title-authoritative-body-v1"


ALLOWED_FEATURES = frozenset(
    {
        "bm25_score",
        "bm25_rank",
        "dense_entity_score",
        "dense_entity_rank",
        "dense_chunk_score",
        "dense_chunk_rank",
        "late_interaction_score",
        "late_interaction_rank",
        "retrieval_text_score",
        "retrieval_text_rank",
        "channel_agreement",
        "query_token_overlap",
        "memory_type_score",
        "lifecycle_score",
    }
)
FORBIDDEN_FEATURE_FRAGMENTS = ("title", "entity_id", "memory_id", "content", "text")


@dataclass(frozen=True)
class RankedCandidate:
    """One candidate represented only by frozen, corpus-independent retrieval features."""

    candidate_id: str
    features: Mapping[str, float]


@dataclass(frozen=True)
class PairwiseExample:
    """A preference pair. ``preferred`` must rank ahead of ``other``."""

    family_id: str
    preferred: RankedCandidate
    other: RankedCandidate


@dataclass(frozen=True)
class ConservativePromotionPolicy:
    """Frozen policy for allowing a cross-encoder to promote one challenger."""

    margin: float
    max_challenger_rank: int
    protect_lexical_score: float
    protect_dual_channel_count: int = 2

    def __post_init__(self) -> None:
        if self.margin < 0:
            raise ValueError("cross-encoder margin cannot be negative")
        if self.max_challenger_rank < 2:
            raise ValueError("max_challenger_rank must include at least one challenger")
        if self.protect_dual_channel_count < 1:
            raise ValueError("protect_dual_channel_count must be positive")


def validate_feature_schema(feature_names: Iterable[str]) -> tuple[str, ...]:
    """Reject identity/text leakage and unknown features before training or scoring."""
    names = tuple(feature_names)
    if not names or len(names) != len(set(names)):
        raise ValueError("feature schema must be non-empty and unique")
    for name in names:
        lowered = name.casefold()
        if any(fragment in lowered for fragment in FORBIDDEN_FEATURE_FRAGMENTS):
            raise ValueError(f"identity/text feature is forbidden: {name}")
        if name not in ALLOWED_FEATURES:
            raise ValueError(f"feature is not in the frozen retrieval schema: {name}")
    return names


def grouped_family_folds(family_ids: Sequence[str], fold_count: int) -> list[set[str]]:
    """Return deterministic folds with each topic family present in exactly one fold."""
    unique = sorted(set(family_ids))
    if fold_count < 2 or fold_count > len(unique):
        raise ValueError("fold_count must be between two and the number of families")
    folds: list[set[str]] = [set() for _ in range(fold_count)]
    for index, family_id in enumerate(unique):
        folds[index % fold_count].add(family_id)
    return folds


def _finite(value: float | int) -> float:
    number = float(value)
    return number if math.isfinite(number) else 0.0


def minmax_normalize(values: Sequence[float]) -> list[float]:
    """Normalize one query/channel without importing cross-query scale information."""
    if not values:
        return []
    finite = [_finite(value) for value in values]
    low, high = min(finite), max(finite)
    if high == low:
        return [0.0] * len(finite)
    return [(value - low) / (high - low) for value in finite]


def normalized_linear_fusion(
    candidates: Sequence[RankedCandidate], channel_weights: Mapping[str, float]
) -> list[RankedCandidate]:
    """Fuse per-query channel scores; ties preserve the retrieval input order."""
    if not candidates:
        return []
    feature_names = validate_feature_schema(channel_weights)
    if any(
        not math.isfinite(float(weight)) or float(weight) < 0 for weight in channel_weights.values()
    ):
        raise ValueError("fusion weights must be finite and non-negative")
    normalized = {
        name: minmax_normalize([candidate.features.get(name, 0.0) for candidate in candidates])
        for name in feature_names
    }
    scored = []
    for index, candidate in enumerate(candidates):
        score = sum(
            normalized[name][index] * float(channel_weights[name]) for name in feature_names
        )
        scored.append((score, index, candidate))
    return [item[2] for item in sorted(scored, key=lambda item: (-item[0], item[1]))]


def train_pairwise_linear_ranker(
    examples: Sequence[PairwiseExample],
    feature_names: Sequence[str],
    *,
    regularization: float = 0.01,
    learning_rate: float = 0.05,
    epochs: int = 100,
) -> dict[str, float]:
    """Train a deterministic regularized pairwise logistic ranker on frozen features."""
    schema = validate_feature_schema(feature_names)
    if not examples:
        raise ValueError("pairwise training requires examples")
    if regularization < 0 or learning_rate <= 0 or epochs < 1:
        raise ValueError("invalid ranker training hyperparameters")
    weights = {name: 0.0 for name in schema}
    for _ in range(epochs):
        for example in sorted(
            examples,
            key=lambda item: (item.family_id, item.preferred.candidate_id, item.other.candidate_id),
        ):
            delta = {
                name: _finite(example.preferred.features.get(name, 0.0))
                - _finite(example.other.features.get(name, 0.0))
                for name in schema
            }
            logit = max(-35.0, min(35.0, sum(weights[name] * delta[name] for name in schema)))
            error = 1.0 / (1.0 + math.exp(logit))
            for name in schema:
                gradient = error * delta[name] - regularization * weights[name]
                weights[name] += learning_rate * gradient
    return weights


def score_linear(candidate: RankedCandidate, weights: Mapping[str, float]) -> float:
    schema = validate_feature_schema(weights)
    return sum(_finite(candidate.features.get(name, 0.0)) * weights[name] for name in schema)


def render_cross_encoder_pair(query: str, title: str, body: str) -> tuple[str, str]:
    """Frozen query/candidate construction shared by every CE contender."""
    normalized_query = " ".join(query.split())
    normalized_title = " ".join(title.split())
    normalized_body = " ".join(body.split())
    if not normalized_query or not (normalized_title or normalized_body):
        raise ValueError("cross-encoder query and candidate text must be non-empty")
    return normalized_query, "\n\n".join(
        part for part in (normalized_title, normalized_body) if part
    )


def _validated_ce_scores(
    ce_scores: Mapping[str, float] | None, candidate_ids: Sequence[str]
) -> tuple[list[float] | None, str]:
    if not ce_scores or any(candidate_id not in ce_scores for candidate_id in candidate_ids):
        return None, "missing_scores"
    try:
        values = [float(ce_scores[candidate_id]) for candidate_id in candidate_ids]
    except (TypeError, ValueError):
        return None, "malformed_scores"
    if any(not math.isfinite(score) for score in values):
        return None, "nonfinite_scores"
    return values, "valid"


def conservative_ce_promote(
    retrieval_order: Sequence[RankedCandidate],
    ce_scores: Mapping[str, float] | None,
    policy: ConservativePromotionPolicy,
    *,
    ambiguity_predicate: Callable[[Sequence[RankedCandidate]], bool],
) -> tuple[list[RankedCandidate], dict[str, object]]:
    """Promote at most one challenger; malformed/tied evidence preserves retrieval order."""
    original = list(retrieval_order)
    diagnostic: dict[str, object] = {"executed": False, "promoted": False, "reason": "stable"}
    if len(original) < 2 or not ambiguity_predicate(original):
        diagnostic["reason"] = "unambiguous"
        return original, diagnostic
    incumbent = original[0]
    lexical = _finite(incumbent.features.get("bm25_score", 0.0))
    agreement = int(_finite(incumbent.features.get("channel_agreement", 0.0)))
    if lexical >= policy.protect_lexical_score or agreement >= policy.protect_dual_channel_count:
        diagnostic["reason"] = "incumbent_protected"
        return original, diagnostic
    challengers = original[1 : policy.max_challenger_rank]
    required_ids = [incumbent.candidate_id, *(item.candidate_id for item in challengers)]
    required_scores, score_status = _validated_ce_scores(ce_scores, required_ids)
    if required_scores is None:
        diagnostic["reason"] = score_status
        return original, diagnostic
    incumbent_score = required_scores[0]
    scored = [
        (score, index, item)
        for index, (item, score) in enumerate(zip(challengers, required_scores[1:]), 1)
    ]
    diagnostic["executed"] = True
    best_score, best_index, best = max(scored, key=lambda item: (item[0], -item[1]))
    observed_margin = best_score - incumbent_score
    diagnostic["margin"] = observed_margin
    if observed_margin <= policy.margin:
        diagnostic["reason"] = "margin_not_met"
        return original, diagnostic
    promoted = [best, incumbent, *original[1:best_index], *original[best_index + 1 :]]
    diagnostic.update(
        {"promoted": True, "reason": "challenger_promoted", "candidate_id": best.candidate_id}
    )
    return promoted, diagnostic
