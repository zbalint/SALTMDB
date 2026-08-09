import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "judge_pool.py"
spec = importlib.util.spec_from_file_location("judge_pool", path)
jp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jp)


class TestJudgePool(unittest.TestCase):
    def setUp(self):
        self.queries = [{"id": "q1", "query": "what is cache?", "source_entity_ids": ["truth"]}]
        self.matrix = {"pools": {"q1": {"truth": {"title": "Truth", "snippet": "secret", "ground_truth_forced_include": True}, "other": {"title": "Other", "snippet": "other"}}}}

    def test_packet_hides_provenance_and_validates_complete_labels(self):
        packet, private = jp.build_judge_packets(self.queries, self.matrix, "codex", "dev", 3)
        rendered = str(packet)
        self.assertNotIn("ground_truth", rendered)
        self.assertNotIn("source_entity_ids", rendered)
        labels = {"labels": [{"task_id": "dev:codex:q1", "candidate_id": c["candidate_id"], "grade": 2} for c in packet["tasks"][0]["candidates"]]}
        normalized = jp.validate_labels(labels, private, "codex")
        self.assertEqual({x["candidate_id"] for x in normalized}, {"truth", "other"})

    def test_rejects_missing_label(self):
        packet, private = jp.build_judge_packets(self.queries, self.matrix, "codex", "dev")
        with self.assertRaises(ValueError):
            jp.validate_labels({"labels": [{"task_id": packet["tasks"][0]["task_id"], "candidate_id": "truth", "grade": 2}]}, private, "codex")


if __name__ == "__main__":
    unittest.main()
