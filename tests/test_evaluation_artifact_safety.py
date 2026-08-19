import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


beq = _load("build_evaluation_queries")
jp = _load("judge_pool")
mj = _load("merge_judgments")
rem = _load("run_evaluation_matrix")
ea = _load("evaluation_artifacts")


class TestEvaluationArtifactSafety(unittest.TestCase):
    def test_query_manifest_tamper_is_rejected(self):
        query = {
            "id": "q1",
            "query": "cache protocol",
            "lang": "en",
            "category": "exact_title",
            "subtype": "fact",
            "split": "dev",
            "source_entity_ids": [],
            "topic_family_id": "family:q1",
            "length_bucket": "short",
            "provenance": "test",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queries.json"
            beq.write_manifest([query], path)
            value = json.loads(path.read_text())
            value["queries"][0]["query"] = "tampered"
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                beq.load_manifest(path)

    def test_luna_packet_rejects_legacy_role_and_mapping_tamper(self):
        queries = [{"id": "q1", "query": "cache", "source_entity_ids": []}]
        matrix = {"pools": {"q1": {"e1": {"title": "Cache", "snippet": "A cache"}}}}
        with self.assertRaises(ValueError):
            jp.build_judge_packets(queries, matrix, "codex", "dev")
        _, mapping = jp.build_judge_packets(queries, matrix, jp.JUDGES[0], "dev")
        mapping["tasks"]["task-0001"]["candidate_ids"].append("candidate-999")
        with self.assertRaises(ValueError):
            jp.validate_labels({"labels": []}, mapping, jp.JUDGES[0])

    def test_merge_rejects_signed_shard_relative_to_matrix(self):
        queries = [{"id": "q1", "query": "cache", "source_entity_ids": []}]
        matrix = {"pools": {"q1": {"e1": {"title": "Cache", "snippet": "A cache"}}}}
        artifacts = []
        for judge in mj.JUDGES:
            artifact = {
                "schema_version": 1,
                "judge": judge,
                "label_count": 0,
                "labels": [],
            }
            artifact["fingerprint"] = mj.artifact_fingerprint(artifact)
            artifacts.append(artifact)
        with self.assertRaises(ValueError):
            mj.merge_all_judgments(queries, artifacts, matrix)

    def test_matrix_contract_rejects_incomplete_configuration_coverage(self):
        query = {"id": "q1"}
        result = {
            "config_rankings": {"q1": {"only": ["e1"]}},
            "pools": {"q1": {"e1": {"ground_truth_forced_include": False}}},
            "errors": [],
        }
        with self.assertRaises(ValueError):
            rem._validate_matrix_contract(
                result, [query], [{"name": "only"}, {"name": "other"}], 20
            )

    def test_run_directory_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ea.run_directory(Path(directory), "../escape")


if __name__ == "__main__":
    unittest.main()
