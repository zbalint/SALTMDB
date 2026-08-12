"""Synthetic-only tests for benchmark retrieval contracts.

These tests intentionally do not load model weights, open a SALTMDB database, or use any
evaluation query/label artifact.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from retrieval_architecture import (  # noqa: E402
    COLBERT_CANDIDATE,
    FASTEMBED_DENSE_CANDIDATES,
    CandidateMetric,
    CompatibilityError,
    Coverage,
    EmbeddingSpec,
    IncompleteCoverageError,
    LateInteractionError,
    RepresentationSpec,
    RetrievalProvenance,
    RetrievalQueryResult,
    RetrievalRun,
    aggregate_entities_by_max_chunk,
    assert_compatible,
    assert_index_compatible,
    plan_isolated_index,
    screen_development_candidates,
)


def representation() -> RepresentationSpec:
    return RepresentationSpec.from_authoritative_text(
        "A title", "Authoritative body", ["A title", "Authoritative body"]
    )


def test_predeclared_candidates_are_metadata_only_and_colbert_is_separate():
    expected = {
        "BAAI/bge-small-en-v1.5",
        "BAAI/bge-base-en-v1.5",
        "BAAI/bge-large-en-v1.5",
        "snowflake/snowflake-arctic-embed-m-long",
        "jinaai/jina-embeddings-v2-base-en",
        "nomic-ai/nomic-embed-text-v1.5",
        "mixedbread-ai/mxbai-embed-large-v1",
        "intfloat/multilingual-e5-large",
    }
    assert len(FASTEMBED_DENSE_CANDIDATES) == 8
    assert {candidate.model_id for candidate in FASTEMBED_DENSE_CANDIDATES} == expected
    assert all(candidate.kind == "dense" for candidate in FASTEMBED_DENSE_CANDIDATES)
    assert COLBERT_CANDIDATE.kind == "late_interaction"
    assert COLBERT_CANDIDATE.model_id == "answerdotai/answerai-colbert-small-v1"
    assert FASTEMBED_DENSE_CANDIDATES[0].query_prefix.startswith("Represent this sentence")
    assert FASTEMBED_DENSE_CANDIDATES[-1].query_prefix == "query: "
    assert FASTEMBED_DENSE_CANDIDATES[-1].document_prefix == "passage: "


def test_embedding_spec_is_frozen_and_requires_revision_or_hash():
    spec = FASTEMBED_DENSE_CANDIDATES[0]
    with pytest.raises((AttributeError, TypeError)):
        spec.dimension = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="revision or model_hash"):
        EmbeddingSpec("model/id", None, 3, tokenizer="tokenizer")


def test_representation_hashes_authoritative_fields_and_rejects_empty_chunks():
    spec = representation()
    assert len(spec.title_hash) == 64
    assert len(spec.body_hash) == 64
    assert len(spec.chunks_hash) == 64
    assert len(spec.retrieval_text_v1_hash) == 64
    with pytest.raises(ValueError, match="at least one chunk"):
        RepresentationSpec.from_authoritative_text("title", "body", [])


def test_compatibility_rejects_prefix_dimension_representation_and_generation_changes():
    base = FASTEMBED_DENSE_CANDIDATES[0]
    changed_prefix = EmbeddingSpec(
        base.model_id,
        base.revision,
        base.dimension,
        "different: ",
        base.document_prefix,
        base.normalization,
        base.tokenizer,
        base.max_input_tokens,
    )
    with pytest.raises(CompatibilityError, match="query_prefix"):
        assert_compatible(base, changed_prefix, representation(), representation(), "g1", "g1")
    with pytest.raises(CompatibilityError, match="generation"):
        assert_compatible(base, base, representation(), representation(), "g1", "g2")
    other_representation = RepresentationSpec.from_authoritative_text("other", "body", ["body"])
    with pytest.raises(CompatibilityError, match="representation"):
        assert_compatible(base, base, representation(), other_representation, "g1", "g1")


def test_index_namespace_is_dimension_specific_and_colbert_is_rejected():
    dense = FASTEMBED_DENSE_CANDIDATES[0]
    plan = plan_isolated_index(dense, representation(), "generation-1")
    assert f"{dense.dimension}d" in plan.namespace
    assert f"{dense.dimension}d" in plan.table_name
    assert_index_compatible(plan, dense, representation(), "generation-1")
    with pytest.raises(CompatibilityError):
        assert_index_compatible(plan, dense, representation(), "generation-2")
    with pytest.raises(LateInteractionError):
        plan_isolated_index(COLBERT_CANDIDATE, representation(), "generation-1")


def test_query_provenance_records_ordered_ids_scores_channels_coverage_and_latency():
    row = RetrievalQueryResult(
        "q1",
        "synthetic query",
        ("entity-a", "entity-b"),
        {"entity-a": 0.9, "entity-b": 0.5},
        {"dense": ["entity-a", "entity-b"], "fts": {"entity-b": 1}},
        Coverage(2, 2),
        latency_ms=3.5,
    )
    assert row.is_complete
    assert row.channel_ranks["dense"]["entity-a"] == 1
    provenance = RetrievalProvenance(
        FASTEMBED_DENSE_CANDIDATES[0], representation(), "g1", "bench_test"
    )
    run = RetrievalRun("run-1", provenance, (row,))
    assert run.results == (row,)
    assert run.to_dict()["provenance"]["compatibility_key"] == run.compatibility_key


def test_incomplete_coverage_is_recorded_but_rejected_by_strict_run_check():
    row = RetrievalQueryResult(
        "q1", "synthetic", ("entity-a",), {"entity-a": 0.1}, {"dense": ["entity-a"]}, 0.5
    )
    assert not row.is_complete
    with pytest.raises(IncompleteCoverageError):
        row.require_complete()
    run = RetrievalRun(
        "run-1",
        RetrievalProvenance(FASTEMBED_DENSE_CANDIDATES[0], representation(), "g1", "bench_test"),
        (row,),
    )
    with pytest.raises(IncompleteCoverageError):
        run.require_complete_coverage()


def test_max_chunk_aggregation_deduplicates_entities_without_global_rrf_bonus():
    rows = [
        {"entity_id": "e1", "chunk_id": "c1", "score": 0.4, "rrf_score": 1000},
        {"entity_id": "e1", "chunk_id": "c2", "score": 0.8, "rrf_score": 0},
        {"entity_id": "e2", "chunk_id": "c1", "score": 0.7, "rrf_score": 9999},
    ]
    aggregates = aggregate_entities_by_max_chunk(rows)
    assert [(row.entity_id, row.max_chunk_score, row.best_chunk_id) for row in aggregates] == [
        ("e1", 0.8, "c2"),
        ("e2", 0.7, "c1"),
    ]


def test_screening_is_accuracy_first_and_colbert_requires_strictly_above_third_dense():
    result = screen_development_candidates(
        [
            CandidateMetric("d1", "dense", 0.90, 90),
            CandidateMetric("d2", "dense", 0.80, 80),
            CandidateMetric("d3", "dense", 0.70, 70),
            CandidateMetric("d4", "dense", 0.60, 1),
            CandidateMetric("colbert", "late_interaction", 0.71, 200),
            CandidateMetric("unsafe", "dense", 0.99, 1, safety_ok=False),
        ]
    )
    assert tuple(row.candidate_id for row in result.dense_top_three) == ("d1", "d2", "d3")
    assert result.selected_ids == ("d1", "d2", "colbert", "d3")
    assert result.discarded["d4"] == "below_dense_top_three"
    assert result.discarded["unsafe"] == "safety"


def test_screening_discards_colbert_on_tie_or_incomplete_coverage():
    result = screen_development_candidates(
        [
            CandidateMetric("d1", "dense", 0.9, 10),
            CandidateMetric("d2", "dense", 0.8, 10),
            CandidateMetric("d3", "dense", 0.7, 10),
            CandidateMetric("colbert", "late_interaction", 0.7, 1),
            CandidateMetric("missing", "dense", 0.95, 1, coverage_complete=False),
        ]
    )
    assert "colbert" not in result.selected_ids
    assert result.discarded["colbert"] == "late_interaction_not_above_third_dense"
    assert result.discarded["missing"] == "incomplete_coverage"
