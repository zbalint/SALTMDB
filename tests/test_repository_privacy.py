"""Regression checks for public-safe benchmark and hook fixtures."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_FIXTURE = ROOT / "hooks/tests/fixtures/real_session_search_memory_turn.jsonl"
SAMPLE_TEXTS = ROOT / "scripts/benchmarking/sample_texts.json"
CORPUS_MANIFEST = ROOT / "scripts/benchmarking/build_evaluation_corpus_manifest.py"
PACKAGE_MANIFEST = ROOT / "MANIFEST.in"
LOCAL_REPLAY_RUNNERS = {
    "scripts/benchmarking/replay_run9_candidate.py",
    "tests/test_replay_run9_candidate.py",
}


def test_session_transcript_fixture_is_entirely_synthetic():
    records = [json.loads(line) for line in SESSION_FIXTURE.read_text().splitlines()]

    assert len(records) >= 10
    assert all(record.get("synthetic_fixture") is True for record in records)
    assert all(len(json.dumps(record)) < 2_000 for record in records)


def test_embedding_benchmark_corpus_is_generated_synthetic_data():
    texts = json.loads(SAMPLE_TEXTS.read_text())

    assert len(texts) == 200
    assert all(text.startswith("[SYNTHETIC BENCHMARK RECORD ") for text in texts)
    assert all("not derived from a user session or memory store" in text for text in texts)


def test_private_agent_experience_plan_is_not_distributed():
    assert not (ROOT / "plans/agent-experience-layer-plan.md").exists()


def test_corpus_manifest_documents_local_private_input_without_naming_it():
    source = CORPUS_MANIFEST.read_text()
    string_constants = "\n".join(
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )

    assert "private source corpus is deliberately not distributed" in string_constants
    assert "locally supplied corpus" in string_constants


def test_local_replay_runners_are_excluded_from_release_archives():
    exclusions = {
        line.removeprefix("exclude ")
        for line in PACKAGE_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.startswith("exclude ")
    }

    assert LOCAL_REPLAY_RUNNERS <= exclusions
