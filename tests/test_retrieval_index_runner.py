import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from retrieval_index_runner import (  # noqa: E402
    DenseIndexRunner,
    IndexDocument,
    IndexRunnerError,
    LateInteractionIndexRunner,
    authoritative_documents,
    fingerprint,
    timed_search,
)


class DenseFake:
    dimension = 2
    compatibility_key = fingerprint("dense-fake-v1")

    def __init__(self):
        self.document_calls = 0

    def embed_documents(self, texts):
        self.document_calls += 1
        return [[float("alpha" in text), float("beta" in text)] for text in texts]

    def embed_query(self, text):
        return [float("alpha" in text), float("beta" in text)]


class LateFake:
    dimension = 2
    compatibility_key = fingerprint("late-fake-v1")

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        result = []
        if "alpha" in text:
            result.append([1.0, 0.0])
        if "beta" in text:
            result.append([0.0, 1.0])
        return result or [[0.0, 0.0]]

    def maxsim(self, query_tokens, document_tokens):
        return sum(
            max(sum(a * b for a, b in zip(q, d)) for d in document_tokens) for q in query_tokens
        )


def document(item_id, entity_id, channel, text):
    return IndexDocument(
        item_id, entity_id, channel, text, fingerprint(entity_id), fingerprint(text)
    )


def test_authoritative_documents_include_title_only_chunk():
    rows = authoritative_documents(
        "e1", "  Useful  Title ", "", [], fingerprint("s"), fingerprint("r")
    )
    assert [(row.channel, row.text) for row in rows] == [
        ("entity", "Useful Title"),
        ("chunk", "Useful Title"),
    ]


def test_dense_build_resume_checksums_and_chunk_max(tmp_path):
    adapter = DenseFake()
    path = tmp_path / "dense.sqlite"
    rows = [
        document("e1", "e1", "entity", "alpha"),
        document("e1:c1", "e1", "chunk", "nothing"),
        document("e1:c2", "e1", "chunk", "alpha beta"),
        document("e2", "e2", "entity", "beta"),
        document("e2:c1", "e2", "chunk", "beta"),
    ]
    with DenseIndexRunner(path, adapter, representation_root=fingerprint("corpus")) as runner:
        receipt = runner.build(rows, batch_size=2)
        assert receipt["ready"] == 1
        assert receipt["completed_count"] == len(rows)
        assert [hit.entity_id for hit in runner.search("alpha", "entity")] == ["e1", "e2"]
        chunk_hits = runner.search("alpha beta", "chunk")
        assert [(hit.entity_id, hit.item_id) for hit in chunk_hits] == [
            ("e1", "e1:c2"),
            ("e2", "e2:c1"),
        ]
        calls = adapter.document_calls
        runner.build(rows, batch_size=2)
        assert adapter.document_calls == calls

        runner.conn.execute("UPDATE dense_vectors SET vector_sha256 = 'bad' WHERE item_id = 'e1'")
        runner.conn.commit()
        runner.build(rows)
        assert adapter.document_calls == calls + 1


def test_dense_sidecar_refuses_cross_model_or_representation_resume(tmp_path):
    path = tmp_path / "dense.sqlite"
    adapter = DenseFake()
    DenseIndexRunner(path, adapter, representation_root=fingerprint("one")).close()
    with pytest.raises(IndexRunnerError, match="identity mismatch"):
        DenseIndexRunner(path, adapter, representation_root=fingerprint("two"))
    other = DenseFake()
    other.compatibility_key = fingerprint("other")
    with pytest.raises(IndexRunnerError, match="identity mismatch"):
        DenseIndexRunner(path, other, representation_root=fingerprint("one"))


def test_dense_search_rejects_corrupted_vector(tmp_path):
    adapter = DenseFake()
    with DenseIndexRunner(
        tmp_path / "dense.sqlite", adapter, representation_root=fingerprint("r")
    ) as runner:
        runner.build([document("e1", "e1", "entity", "alpha")])
        runner.conn.execute("UPDATE dense_vectors SET vector = ? WHERE item_id = 'e1'", (b"bad",))
        runner.conn.commit()
        with pytest.raises(IndexRunnerError, match="checksum"):
            runner.search("alpha", "entity")


def test_late_interaction_is_separate_and_maxsim_ranked(tmp_path):
    adapter = LateFake()
    rows = [
        document("e1", "e1", "entity", "alpha beta"),
        document("e2", "e2", "entity", "alpha"),
    ]
    with LateInteractionIndexRunner(
        tmp_path / "late.sqlite", adapter, representation_root=fingerprint("r")
    ) as runner:
        receipt = runner.build(rows)
        assert receipt["kind"] == "late_interaction"
        hits = runner.search("alpha beta")
        assert [hit.entity_id for hit in hits] == ["e1", "e2"]
        expected = [
            adapter.maxsim(adapter.embed_query("alpha beta"), adapter.embed_query("alpha beta")),
            adapter.maxsim(adapter.embed_query("alpha beta"), adapter.embed_query("alpha")),
        ]
        assert [hit.score for hit in hits] == pytest.approx(expected)
        assert [hit.entity_id for hit in runner.search_subset("alpha beta", ["e2", "e1"])] == [
            "e1",
            "e2",
        ]
        with pytest.raises(IndexRunnerError, match="must not be empty"):
            runner.search_subset("alpha", [])
        with pytest.raises(IndexRunnerError, match="entity documents only"):
            runner.build([document("e1:c", "e1", "chunk", "alpha")])


class LateFakeNumpyMatrix:
    """Returns real numpy 2D arrays, matching retrieval_adapters.LateInteractionEmbeddingAdapter.

    ``LateFake`` above returns plain Python lists of lists, which never reproduces the real
    ``if not matrix:`` truth-value-ambiguous crash: that only fires for a numpy array with more
    than one row, which is exactly what the real adapter (via ``_validate_late_matrix``) returns
    for every genuine multi-token document. This fake closes that gap.
    """

    dimension = 2
    compatibility_key = fingerprint("late-fake-numpy-v1")

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        # Every real document/query has at least one token; two rows is the common case and is
        # exactly the shape that made ``if not matrix`` ambiguous.
        return np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    def maxsim(self, query_tokens, document_tokens):
        return sum(
            max(sum(a * b for a, b in zip(q, d)) for d in document_tokens) for q in query_tokens
        )


def test_late_interaction_build_accepts_real_numpy_token_matrices(tmp_path):
    """Regression test: a genuine numpy multi-row matrix must not raise a truth-value error.

    Reproduces the exact failure mode found running the real ColBERT contender in Gate D:
    ``if not matrix`` on a numpy array with more than one element raises
    ``ValueError: truth value of an array with more than one element is ambiguous``.
    """
    adapter = LateFakeNumpyMatrix()
    with LateInteractionIndexRunner(
        tmp_path / "late_numpy.sqlite", adapter, representation_root=fingerprint("r")
    ) as runner:
        receipt = runner.build([document("e1", "e1", "entity", "alpha beta")])
        assert receipt["kind"] == "late_interaction"
        row = runner.conn.execute(
            "SELECT token_count FROM token_vectors WHERE item_id = 'e1'"
        ).fetchone()
        assert row["token_count"] == 2


def test_nonfinite_and_wrong_dimensions_fail_closed(tmp_path):
    adapter = DenseFake()
    adapter.embed_documents = lambda _texts: [[float("nan"), 0.0]]
    with DenseIndexRunner(
        tmp_path / "bad.sqlite", adapter, representation_root=fingerprint("r")
    ) as runner:
        with pytest.raises(IndexRunnerError, match="non-finite"):
            runner.build([document("e", "e", "entity", "alpha")])


def test_timed_search_records_failure_without_raising():
    result, latency, error = timed_search(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert result == []
    assert latency >= 0
    assert error == "RuntimeError: boom"
