import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "build_evaluation_queries.py"
)
_SPEC = importlib.util.spec_from_file_location("build_evaluation_queries", _PATH)
beq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(beq)


def _slot(number, family=None):
    return {
        "slot_id": f"s{number}",
        "query_id": f"q{number}",
        "instruction": "Write a search query",
        "source_text": f"Source text {number}",
        "target_language": "English",
        "lang": "en",
        "category": "exact_title",
        "subtype": "fact",
        "source_entity_ids": [f"e{number}"],
        "topic_family_id": family or f"family:{number}",
        "provenance": "llm:codex",
    }


def _quota_slots():
    slots = []
    number = 1
    for split in ("dev", "blind"):
        for category, count in beq.EVALUATION_CATEGORY_TARGETS[split].items():
            # Use one family per slot so this fixture isolates quota accounting from
            # source-selection details; multi-slot-family reconstruction is tested below.
            for _ in range(count):
                item = _slot(number, family=f"{category}:{number}")
                item["category"] = category
                if category in beq.NEGATIVE_CATEGORIES:
                    item["source_text"] = ""
                    item["source_entity_ids"] = []
                slots.append(item)
                number += 1
    return slots


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
            beq.materialize_queries(
                slots, [{"slot_id": "s1", "query": "one"}, {"slot_id": "s1", "query": "two"}]
            )

    def test_manifest_rejects_cross_split_family(self):
        slots = [_slot(1, "same"), _slot(2, "same")]
        assigned = [{**slots[0], "split": "dev"}, {**slots[1], "split": "blind"}]
        with self.assertRaises(ValueError):
            beq.materialize_queries(
                assigned, [{"slot_id": "s1", "query": "one"}, {"slot_id": "s2", "query": "two"}]
            )

    def test_frozen_corpus_selector_keeps_relation_family_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen-copy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE entities (id TEXT, title TEXT, full_content TEXT, memory_type TEXT, status TEXT)"
            )
            conn.execute(
                "CREATE TABLE relations (source_id TEXT, target_id TEXT, predicate TEXT, valid_to TEXT)"
            )
            conn.executemany(
                "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
                [(f"e{i}", f"Title {i}", f"Body {i}", "fact", "raw") for i in range(5)],
            )
            conn.execute("INSERT INTO relations VALUES ('e0', 'e1', 'supersedes', NULL)")
            conn.commit()
            conn.close()
            slots = beq.build_source_slots_from_corpus(path, positive_total=4, negative_total=6)
        self.assertEqual(len(slots), 10)
        self.assertEqual(sum(item["category"] in beq.NEGATIVE_CATEGORIES for item in slots), 6)
        self.assertTrue(any(item["topic_family_id"].startswith("cluster:") for item in slots))

    def test_full_quota_assignment_is_exact_and_family_safe(self):
        assigned = beq.assign_slots(_quota_slots(), 400, 800)
        counts = {split: {} for split in ("dev", "blind")}
        families = {}
        for slot in assigned:
            family = slot["topic_family_id"]
            previous = families.setdefault(family, slot["split"])
            self.assertEqual(previous, slot["split"])
            category = slot["category"]
            counts[slot["split"]][category] = counts[slot["split"]].get(category, 0) + 1
        self.assertEqual(counts, beq.EVALUATION_CATEGORY_TARGETS)

    def test_quota_assignment_rejects_mixed_category_family(self):
        first = _slot(1, family="mixed")
        second = _slot(2, family="mixed")
        second["category"] = "paraphrase"
        with self.assertRaises(ValueError):
            beq.assign_slots(
                [first, second],
                1,
                1,
                category_targets={"dev": {"exact_title": 1}, "blind": {"exact_title": 1}},
            )


if __name__ == "__main__":
    unittest.main()
