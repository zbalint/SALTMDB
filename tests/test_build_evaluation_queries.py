import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "build_evaluation_queries.py"
_SPEC = importlib.util.spec_from_file_location("build_evaluation_queries", _PATH)
beq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(beq)


def _slot(number, family=None):
    return {"slot_id": f"s{number}", "query_id": f"q{number}", "instruction": "Write a search query",
            "source_text": f"Source text {number}", "target_language": "English", "lang": "en",
            "category": "exact_title", "subtype": "fact", "source_entity_ids": [f"e{number}"],
            "topic_family_id": family or f"family:{number}", "provenance": "llm:codex"}


class TestBuildEvaluationQueries(unittest.TestCase):
    def test_canonical_slot_rejects_provenance_extra_field(self):
        slot = _slot(1)
        slot["private_note"] = "do not disclose"
        with self.assertRaises(ValueError):
            beq.validate_slots([slot])

    def test_batches_are_bounded_and_provenance_safe(self):
        slots = [_slot(i) for i in range(61)]
        batches = beq.build_batches(slots)
        self.assertEqual([len(batch) for batch in batches], [60, 1])
        self.assertNotIn("source_entity_ids", batches[0][0])
        self.assertNotIn("topic_family_id", batches[0][0])

    def test_materialize_rejects_duplicate_result_slot(self):
        slots = [_slot(1)]
        with self.assertRaises(ValueError):
            beq.materialize_queries(slots, [{"slot_id": "s1", "query": "one"}, {"slot_id": "s1", "query": "two"}])

    def test_manifest_rejects_cross_split_family(self):
        slots = [_slot(1, "same"), _slot(2, "same")]
        assigned = [{**slots[0], "split": "dev"}, {**slots[1], "split": "blind"}]
        with self.assertRaises(ValueError):
            beq.materialize_queries(assigned, [{"slot_id": "s1", "query": "one"}, {"slot_id": "s2", "query": "two"}])

    def test_frozen_corpus_selector_keeps_relation_family_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen-copy.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE entities (id TEXT, title TEXT, full_content TEXT, memory_type TEXT, status TEXT)")
            conn.execute("CREATE TABLE relations (source_id TEXT, target_id TEXT, predicate TEXT, valid_to TEXT)")
            conn.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?)", [
                (f"e{i}", f"Title {i}", f"Body {i}", "fact", "raw") for i in range(5)
            ])
            conn.execute("INSERT INTO relations VALUES ('e0', 'e1', 'supersedes', NULL)")
            conn.commit()
            conn.close()
            slots = beq.build_source_slots_from_corpus(path, positive_total=4, negative_total=6)
        self.assertEqual(len(slots), 10)
        self.assertEqual(sum(item["category"] in beq.NEGATIVE_CATEGORIES for item in slots), 6)
        self.assertTrue(any(item["topic_family_id"].startswith("cluster:") for item in slots))


if __name__ == "__main__":
    unittest.main()
