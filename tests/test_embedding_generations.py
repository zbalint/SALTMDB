import sqlite3

import pytest

from saltmdb.db.embedding_generations import (
    GenerationSpec,
    activate_generation,
    generation_table_name,
    init_embedding_generation_schema,
    mark_generation_ready,
    record_generation_vector,
    register_generation,
    validate_active_generation,
)


def spec(model: str = "model-a", dimension: int = 3) -> GenerationSpec:
    return GenerationSpec(
        model, "revision-1", dimension, "query: ", "passage: ", "l2", "body", "a" * 64
    )


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_embedding_generation_schema(conn)
    return conn


def test_generation_names_are_dimension_specific_and_injection_safe():
    assert generation_table_name("winner-1", 384).endswith("_d384")
    with pytest.raises(ValueError):
        generation_table_name('winner";DROP TABLE entities', 384)


def test_generation_rejects_cross_model_reuse_and_wrong_dimension():
    conn = connection()
    register_generation(conn, "g1", spec(), expected_count=1)
    with pytest.raises(ValueError, match="incompatible"):
        register_generation(conn, "g1", spec("model-b"), expected_count=1)
    with pytest.raises(ValueError, match="dimension"):
        record_generation_vector(conn, "g1", "entity", "hash", [1.0, 2.0])


def test_interrupted_generation_cannot_activate_and_resume_is_checksum_stable():
    conn = connection()
    register_generation(conn, "g1", spec(), expected_count=2)
    checksum = record_generation_vector(conn, "g1", "one", "source-one", [1.0, 0.0, 0.0])
    assert record_generation_vector(conn, "g1", "one", "source-one", [1.0, 0.0, 0.0]) == checksum
    with pytest.raises(ValueError, match="coverage"):
        mark_generation_ready(conn, "g1", "health")
    with pytest.raises(ValueError, match="not ready"):
        activate_generation(conn, "g1")


def test_atomic_activation_validation_and_rollback():
    conn = connection()
    for generation_id, model in (("g1", "model-a"), ("g2", "model-b")):
        current = spec(model)
        register_generation(conn, generation_id, current, expected_count=1)
        record_generation_vector(conn, generation_id, "entity", "source", [1.0, 0.0, 0.0])
        mark_generation_ready(conn, generation_id, f"health-{generation_id}")
    activate_generation(conn, "g1")
    assert validate_active_generation(conn, spec("model-a")) == "g1"
    with pytest.raises(RuntimeError, match="mismatch"):
        validate_active_generation(conn, spec("model-b"))
    activate_generation(conn, "g2")
    assert validate_active_generation(conn, spec("model-b")) == "g2"
    activate_generation(conn, "g1", reason="rollback after post-switch check")
    assert validate_active_generation(conn, spec("model-a")) == "g1"
