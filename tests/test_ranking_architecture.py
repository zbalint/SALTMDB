import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from ranking_architecture import (  # noqa: E402
    CROSS_ENCODER_MODELS,
    ConservativePromotionPolicy,
    PairwiseExample,
    RankedCandidate,
    conservative_ce_promote,
    grouped_family_folds,
    normalized_linear_fusion,
    render_cross_encoder_pair,
    score_linear,
    train_pairwise_linear_ranker,
    validate_feature_schema,
)


def candidate(candidate_id: str, **features: float) -> RankedCandidate:
    return RankedCandidate(candidate_id, features)


def test_feature_schema_forbids_identity_and_text_leakage():
    with pytest.raises(ValueError, match="identity/text"):
        validate_feature_schema(["entity_id"])
    with pytest.raises(ValueError, match="identity/text"):
        validate_feature_schema(["title_overlap"])
    assert validate_feature_schema(["bm25_score", "lifecycle_score"])


def test_grouped_folds_are_disjoint_and_complete():
    folds = grouped_family_folds(["b", "a", "a", "d", "c"], 3)
    assert set.union(*folds) == {"a", "b", "c", "d"}
    assert all(
        left.isdisjoint(right) for index, left in enumerate(folds) for right in folds[index + 1 :]
    )


def test_normalized_fusion_is_per_query_and_tie_stable():
    rows = [
        candidate("a", bm25_score=1),
        candidate("b", bm25_score=3),
        candidate("c", bm25_score=3),
    ]
    assert [row.candidate_id for row in normalized_linear_fusion(rows, {"bm25_score": 1})] == [
        "b",
        "c",
        "a",
    ]


def test_pairwise_ranker_learns_preference_without_identity_features():
    preferred = candidate("winner", bm25_score=1, lifecycle_score=1)
    other = candidate("other", bm25_score=0, lifecycle_score=0)
    weights = train_pairwise_linear_ranker(
        [PairwiseExample("family-a", preferred, other)],
        ["bm25_score", "lifecycle_score"],
        epochs=30,
    )
    assert score_linear(preferred, weights) > score_linear(other, weights)


def test_ce_protects_lexical_winner_and_promotes_at_most_one():
    rows = [
        candidate("incumbent", bm25_score=0.95, channel_agreement=1),
        candidate("challenger", bm25_score=0.1),
        candidate("third", bm25_score=0.0),
    ]
    protected = ConservativePromotionPolicy(0.2, 3, protect_lexical_score=0.9)
    order, diagnostic = conservative_ce_promote(
        rows,
        {"incumbent": 0.0, "challenger": 2.0, "third": 3.0},
        protected,
        ambiguity_predicate=lambda _: True,
    )
    assert order == rows
    assert diagnostic["reason"] == "incumbent_protected"

    promotable = ConservativePromotionPolicy(
        0.2, 3, protect_lexical_score=1.0, protect_dual_channel_count=2
    )
    order, diagnostic = conservative_ce_promote(
        rows,
        {"incumbent": 0.0, "challenger": 2.0, "third": 1.0},
        promotable,
        ambiguity_predicate=lambda _: True,
    )
    assert [item.candidate_id for item in order] == ["challenger", "incumbent", "third"]
    assert diagnostic["promoted"] is True


def test_ce_ties_missing_and_malformed_scores_preserve_order():
    rows = [candidate("a", bm25_score=0), candidate("b", bm25_score=0)]
    policy = ConservativePromotionPolicy(0.0, 2, protect_lexical_score=1.0)
    for scores in (None, {"a": 1.0}, {"a": float("nan"), "b": float("nan")}, {"a": 1.0, "b": 1.0}):
        order, diagnostic = conservative_ce_promote(
            rows, scores, policy, ambiguity_predicate=lambda _: True
        )
        assert order == rows
        assert diagnostic["promoted"] is False

    for scores in (
        {"a": float("nan"), "b": 10.0},
        {"a": 0.0, "b": float("inf")},
        {"a": 0.0, "b": "bad"},
    ):
        order, diagnostic = conservative_ce_promote(
            rows, scores, policy, ambiguity_predicate=lambda _: True
        )
        assert order == rows
        assert diagnostic["promoted"] is False


def test_ce_model_set_and_text_renderer_are_frozen():
    assert len(CROSS_ENCODER_MODELS) == 5
    assert len(set(CROSS_ENCODER_MODELS)) == 5
    assert render_cross_encoder_pair("  query  text ", " A title ", " body   text ") == (
        "query text",
        "A title\n\nbody text",
    )
