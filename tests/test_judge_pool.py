import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "judge_pool.py"
spec = importlib.util.spec_from_file_location("judge_pool", path)
jp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jp)


class TestJudgePool(unittest.TestCase):
    def setUp(self):
        self.queries = [{"id": "q1", "query": "what is cache?", "source_entity_ids": ["truth"]}]
        self.matrix = {
            "pools": {
                "q1": {
                    "truth": {
                        "title": "Truth",
                        "snippet": "secret",
                        "ground_truth_forced_include": True,
                    },
                    "other": {"title": "Other", "snippet": "other"},
                }
            }
        }

    def test_packet_hides_provenance_and_validates_complete_labels(self):
        packet, private = jp.build_judge_packets(
            self.queries, self.matrix, "agent_eval_judge_a", "dev", 3
        )
        rendered = str(packet)
        self.assertNotIn("dev", rendered)
        self.assertNotIn("q1", rendered)
        self.assertNotIn("truth", rendered)
        self.assertNotIn("ground_truth", rendered)
        self.assertNotIn("source_entity_ids", rendered)
        self.assertEqual(
            packet["rubric"],
            {
                "0": "Irrelevant or non-answer to the query.",
                "1": "Related context or partial relevance, but not a direct answer to the query.",
                "2": "Directly answers the query.",
            },
        )
        labels = {
            "labels": [
                {
                    "task_id": packet["tasks"][0]["task_id"],
                    "candidate_id": c["candidate_id"],
                    "grade": 2,
                }
                for c in packet["tasks"][0]["candidates"]
            ]
        }
        normalized = jp.validate_labels(labels, private, "agent_eval_judge_a")
        self.assertEqual({x["candidate_id"] for x in normalized}, {"truth", "other"})

    def test_rejects_missing_label(self):
        packet, private = jp.build_judge_packets(
            self.queries, self.matrix, "agent_eval_judge_a", "dev"
        )
        with self.assertRaises(ValueError):
            jp.validate_labels(
                {
                    "labels": [
                        {
                            "task_id": packet["tasks"][0]["task_id"],
                            "candidate_id": "candidate-001",
                            "grade": 2,
                        }
                    ]
                },
                private,
                "agent_eval_judge_a",
            )

    def test_blind_packet_gate_rejects_missing_or_invalid_shortlist(self):
        with self.assertRaisesRegex(RuntimeError, "signed --dev-shortlist"):
            jp.require_frozen_dev_shortlist(None)
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid = Path(temp_dir) / "invalid-shortlist.json"
            invalid.write_text(json.dumps({}))
            with self.assertRaises(ValueError):
                jp.require_frozen_dev_shortlist(invalid)


if __name__ == "__main__":
    unittest.main()
