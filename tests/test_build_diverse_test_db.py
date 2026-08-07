"""Automated tests for scripts/benchmarking/build_diverse_test_db.py's pure/importable logic.

Uses tiny synthetic fixtures (a handful of fake .md files in a temp dir) -- never the real
test_data/ corpus, so this suite runs in seconds. See the plan this implements
(~/.claude/plans/cheeky-plotting-tulip.md, Rev 6) "Verification" section, items 1-9.
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "build_diverse_test_db.py"
)
_spec = importlib.util.spec_from_file_location("build_diverse_test_db", _MODULE_PATH)
bdt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdt)


def _write_doc(dataset_dir: Path, filename: str, frontmatter: dict, body: str) -> Path:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"] + [f"{k}: {v}" for k, v in frontmatter.items()] + ["---", "", body]
    path = dataset_dir / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# Real, non-repetitive prose -- store_memory's quality gate rejects both too-short payloads and
# high n-gram repetition, so fixture bodies can't just be a repeated token.
_PROSE = [
    "The distributed cache invalidation protocol propagates updates asynchronously across all "
    "replica nodes in the cluster, each maintaining a local version vector.",
    "A leader election algorithm based on raft consensus prevents split-brain scenarios during a "
    "network partition, and throughput scales roughly linearly as shards are added.",
    "Snapshot isolation is achieved by pinning a read timestamp at the start of every "
    "transaction, while background compaction periodically merges tombstoned entries.",
    "Operators can tune the gossip fan-out factor to trade convergence latency against network "
    "overhead, and a circuit breaker trips when downstream error rates exceed a threshold.",
    "Client libraries automatically retry idempotent requests against a different replica on "
    "timeout, and configuration changes roll out gradually via canary deployment.",
]


class TestSelectTopN(unittest.TestCase):
    """Item 2: select_top_n(seed, 20) is a subset of select_top_n(seed, 40)."""

    def test_stable_prefix_property_various_shapes(self):
        for n_candidates in (5, 25, 60, 100):
            candidates = [f"dataset/chunk_{i:03d}/doc_{i:06d}_part_000.md" for i in range(n_candidates)]
            sel20 = bdt.select_top_n(candidates, "seed-x", min(20, n_candidates))
            sel40 = bdt.select_top_n(candidates, "seed-x", min(40, n_candidates))
            self.assertLessEqual(set(sel20), set(sel40))
            self.assertEqual(sel40[: len(sel20)], sel20)

    def test_n_none_returns_full_stable_order_no_truncation(self):
        candidates = [f"doc_{i:06d}_part_000.md" for i in range(10)]
        full = bdt.select_top_n(candidates, "seed-y", None)
        self.assertEqual(len(full), 10)
        self.assertEqual(set(full), set(candidates))
        # Same seed -> same order every call (determinism, not just correct membership).
        self.assertEqual(full, bdt.select_top_n(candidates, "seed-y", None))

    def test_different_seed_different_order_same_membership(self):
        candidates = [f"doc_{i:06d}_part_000.md" for i in range(20)]
        a = bdt.select_top_n(candidates, "seed-a", None)
        b = bdt.select_top_n(candidates, "seed-b", None)
        self.assertEqual(set(a), set(b))
        self.assertNotEqual(a, b)


class TestSplitGroupId(unittest.TestCase):
    """Item 8: split_group_id groups title-bearing docs by (dataset, title); falls back to
    original_document_id for title-less datasets."""

    def test_title_bearing_docs_share_split_group_id(self):
        # squad/duplicate-download shape: same source_title, different source_document_ids.
        doc_a = bdt.ParsedDoc(
            dataset="wikipedia_spanish",
            source_relpath="wikipedia_spanish/chunk_012/doc_012281_part_000.md",
            source_document_id="012281",
            part_index=0,
            body="...",
            source_dataset_field="wikipedia_spanish",
            hf_label=None,
            source_title="Roman Holiday",
            source_url="https://es.wikipedia.org/wiki/Roman_Holiday",
        )
        doc_b = bdt.ParsedDoc(
            dataset="wikipedia_spanish",
            source_relpath="wikipedia_spanish/chunk_025/doc_025681_part_000.md",
            source_document_id="025681",
            part_index=0,
            body="...",
            source_dataset_field="wikipedia_spanish",
            hf_label=None,
            source_title="Roman Holiday",
            source_url="https://es.wikipedia.org/wiki/Roman_Holiday",
        )
        meta_a = bdt.build_metadata(doc_a, "run-1")
        meta_b = bdt.build_metadata(doc_b, "run-1")
        self.assertEqual(meta_a["split_group_id"], meta_b["split_group_id"])
        self.assertEqual(meta_a["split_group_id"], "wikipedia_spanish:Roman Holiday")
        # original_document_id itself must differ -- that's exactly the bug Rev 5 had.
        self.assertNotEqual(meta_a["original_document_id"], meta_b["original_document_id"])

    def test_non_title_dataset_falls_back_to_original_document_id_per_doc(self):
        doc_a = bdt.ParsedDoc(
            dataset="ag_news",
            source_relpath="ag_news/chunk_071/doc_071021_part_000.md",
            source_document_id="071021",
            part_index=0,
            body="...",
            source_dataset_field="ag_news",
            hf_label=0,
            source_title=None,
            source_url=None,
        )
        doc_b = bdt.ParsedDoc(
            dataset="ag_news",
            source_relpath="ag_news/chunk_071/doc_071022_part_000.md",
            source_document_id="071022",
            part_index=0,
            body="...",
            source_dataset_field="ag_news",
            hf_label=1,
            source_title=None,
            source_url=None,
        )
        meta_a = bdt.build_metadata(doc_a, "run-1")
        meta_b = bdt.build_metadata(doc_b, "run-1")
        self.assertEqual(meta_a["split_group_id"], meta_a["original_document_id"])
        self.assertEqual(meta_a["split_group_id"], "ag_news:071021")
        self.assertNotEqual(meta_a["split_group_id"], meta_b["split_group_id"])


class TestFrontmatterParsing(unittest.TestCase):
    """Items 6 and 7: malformed/unsupported files are counted outcomes, not exceptions; a
    successfully parsed doc's metadata shape matches Step 4 exactly."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unsupported_filename_is_counted_not_raised(self):
        d = self.temp_dir / "ag_news"
        d.mkdir()
        path = d / "not_a_matching_name.md"
        path.write_text("---\nsource_dataset: ag_news\n---\n\nbody", encoding="utf-8")
        result = bdt.parse_frontmatter_file(path, "ag_news", "ag_news/not_a_matching_name.md")
        self.assertEqual(result.outcome, "unsupported_filename")
        self.assertIsNone(result.doc)
        self.assertIsNotNone(result.error_detail)

    def test_missing_closing_delimiter_is_malformed(self):
        d = self.temp_dir / "ag_news"
        d.mkdir()
        path = d / "doc_000001_part_000.md"
        path.write_text("---\nsource_dataset: ag_news\n\nbody with no closing marker", encoding="utf-8")
        result = bdt.parse_frontmatter_file(path, "ag_news", "ag_news/doc_000001_part_000.md")
        self.assertEqual(result.outcome, "malformed_file")
        self.assertIsNotNone(result.error_detail)

    def test_non_utf8_bytes_is_malformed(self):
        d = self.temp_dir / "wikipedia_russian"
        d.mkdir()
        path = d / "doc_000002_part_000.md"
        # Truncated multi-byte UTF-8 sequence (lone continuation byte) -- exactly the class of
        # corruption the plan flags as a real risk for the non-English sets.
        path.write_bytes(b"---\nsource_dataset: wikipedia_russian\n---\n\n\xff\xfe not valid utf-8")
        result = bdt.parse_frontmatter_file(path, "wikipedia_russian", "wikipedia_russian/doc_000002_part_000.md")
        self.assertEqual(result.outcome, "malformed_file")
        self.assertIn("utf-8", result.error_detail.lower())

    def test_missing_source_dataset_field_is_malformed(self):
        d = self.temp_dir / "ag_news"
        d.mkdir()
        path = d / "doc_000003_part_000.md"
        path.write_text("---\nlabel: 1\n---\n\nsome body text here", encoding="utf-8")
        result = bdt.parse_frontmatter_file(path, "ag_news", "ag_news/doc_000003_part_000.md")
        self.assertEqual(result.outcome, "malformed_file")

    def test_squad_pickle_yaml_never_parsed_as_yaml(self):
        """The actual squad shape: an `answers` field containing a numpy pickle tag that a real
        YAML loader would choke on -- must be silently ignored, not crash the parser."""
        d = self.temp_dir / "squad"
        d.mkdir()
        path = d / "doc_000004_part_000.md"
        path.write_text(
            "---\n"
            "answers:\n"
            "  answer_start: !!python/object/apply:numpy._core.multiarray._reconstruct\n"
            "    args:\n"
            "    - &id001 !!python/name:numpy.ndarray ''\n"
            "source_dataset: squad\n"
            "title: Some Article\n"
            "---\n\n"
            "The body text of this squad QA-pair row.",
            encoding="utf-8",
        )
        result = bdt.parse_frontmatter_file(path, "squad", "squad/doc_000004_part_000.md")
        self.assertEqual(result.outcome, "parsed")
        self.assertEqual(result.doc.source_title, "Some Article")


class TestMultipartResume(unittest.TestCase):
    """Item 1: two files sharing source_document_id (different part_index) get distinct
    record_keys and both get ingested -- a resume with only one checkpointed does not skip
    the other."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.test_data_dir = self.temp_dir / "test_data"
        _write_doc(
            self.test_data_dir / "ag_news",
            "doc_000100_part_000.md",
            {"label": 0, "source_dataset": "ag_news"},
            _PROSE[0],
        )
        _write_doc(
            self.test_data_dir / "ag_news",
            "doc_000100_part_001.md",
            {"label": 0, "source_dataset": "ag_news"},
            _PROSE[1],
        )
        self.db_path = str(self.temp_dir / "dest.db")
        init_db(self.db_path).close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_partial_checkpoint_does_not_skip_sibling_part(self):
        dataset_dir = self.test_data_dir / "ag_news"
        candidates = bdt.enumerate_candidates(dataset_dir)
        self.assertEqual(len(candidates), 2)
        selected = bdt.select_top_n(candidates, "seed", None)
        self.assertEqual(len(selected), 2)
        self.assertNotEqual(selected[0], selected[1])

        # Pre-seed the checkpoint with only ONE of the two record_keys already processed.
        state = bdt.CheckpointState()
        state.add(
            {
                "record_key": selected[0],
                "dataset": "ag_news",
                "outcome": "stored_clean",
                "ingestion_run_id": "prior-run",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        checkpoint_path = Path(f"{self.db_path}.checkpoint.json")
        bdt.write_checkpoint(checkpoint_path, state)

        manifest = bdt.run_ingestion(
            dest_db_path=self.db_path,
            datasets=["ag_news"],
            n_per_dataset=None,
            seed="seed",
            test_data_dir=self.test_data_dir,
            checkpoint_every=250,
            run_id="this-run",
        )
        ds_stats = manifest["datasets"]["ag_news"]
        self.assertEqual(ds_stats["resumed_skipped"], 1)
        self.assertEqual(manifest["attempted_this_invocation"], 1)
        self.assertEqual(manifest["attempted_cumulative"], 2)

        final_state = bdt.load_checkpoint(checkpoint_path)
        self.assertEqual(final_state.processed_keys, set(selected))


class TestResumeGrowingN(unittest.TestCase):
    """Item 3: resume from n=20 to n=40 attempts exactly 20 new records, reprocesses none of
    the original 20."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.test_data_dir = self.temp_dir / "test_data"
        for i in range(50):
            _write_doc(
                self.test_data_dir / "ag_news",
                f"doc_{i:06d}_part_000.md",
                {"label": i % 4, "source_dataset": "ag_news"},
                _PROSE[i % len(_PROSE)] + f" (variant {i})",
            )
        self.db_path = str(self.temp_dir / "dest.db")
        init_db(self.db_path).close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resume_20_to_40_attempts_only_20_new(self):
        m1 = bdt.run_ingestion(
            dest_db_path=self.db_path,
            datasets=["ag_news"],
            n_per_dataset=20,
            seed="seed",
            test_data_dir=self.test_data_dir,
            checkpoint_every=250,
            run_id="run-1",
        )
        self.assertEqual(m1["datasets"]["ag_news"]["selected"], 20)
        self.assertEqual(m1["attempted_this_invocation"], 20)
        self.assertEqual(m1["datasets"]["ag_news"]["resumed_skipped"], 0)

        checkpoint_path = Path(f"{self.db_path}.checkpoint.json")
        state_after_1 = bdt.load_checkpoint(checkpoint_path)
        original_20 = set(state_after_1.processed_keys)
        self.assertEqual(len(original_20), 20)

        m2 = bdt.run_ingestion(
            dest_db_path=self.db_path,
            datasets=["ag_news"],
            n_per_dataset=40,
            seed="seed",
            test_data_dir=self.test_data_dir,
            checkpoint_every=250,
            run_id="run-2",
        )
        self.assertEqual(m2["datasets"]["ag_news"]["selected"], 40)
        self.assertEqual(m2["attempted_this_invocation"], 20)
        self.assertEqual(m2["datasets"]["ag_news"]["resumed_skipped"], 20)

        state_after_2 = bdt.load_checkpoint(checkpoint_path)
        self.assertEqual(len(state_after_2.processed_keys), 40)
        # None of the original 20 were reprocessed: every record for a key in original_20 still
        # has exactly one checkpoint entry, and it's still stamped with run-1.
        by_key = {}
        for rec in state_after_2.records:
            by_key.setdefault(rec["record_key"], []).append(rec)
        for key in original_20:
            self.assertEqual(len(by_key[key]), 1)
            self.assertEqual(by_key[key][0]["ingestion_run_id"], "run-1")


class TestDestinationGuard(unittest.TestCase):
    """Item 4: path guard behaviors, including WAL/SHM sidecar handling."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.live_path = self.temp_dir / "live.db"
        init_db(str(self.live_path)).close()
        self.source_path = self.temp_dir / "source.db"
        init_db(str(self.source_path)).close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_same_path_rejected(self):
        with self.assertRaises(bdt.SamePathError):
            bdt.resolve_and_guard_destination(
                str(self.source_path), str(self.source_path), False, False, str(self.live_path)
            )

    def test_dest_equals_live_rejected(self):
        with self.assertRaises(bdt.RefuseLiveDBError):
            bdt.resolve_and_guard_destination(
                str(self.source_path), str(self.live_path), False, False, str(self.live_path)
            )

    def test_symlink_to_live_rejected(self):
        link = self.temp_dir / "dest_link_live.db"
        link.symlink_to(self.live_path)
        with self.assertRaises(bdt.RefuseLiveDBError):
            bdt.resolve_and_guard_destination(
                str(self.source_path), str(link), False, False, str(self.live_path)
            )

    def test_symlink_to_source_rejected(self):
        link = self.temp_dir / "dest_link_source.db"
        link.symlink_to(self.source_path)
        with self.assertRaises(bdt.SamePathError):
            bdt.resolve_and_guard_destination(
                str(self.source_path), str(link), False, False, str(self.live_path)
            )

    def test_existing_destination_without_overwrite_or_resume_rejected(self):
        dest = self.temp_dir / "dest.db"
        dest.write_text("pre-existing")
        with self.assertRaises(bdt.DestinationExistsError):
            bdt.resolve_and_guard_destination(
                str(self.source_path), str(dest), False, False, str(self.live_path)
            )

    def test_existing_destination_with_overwrite_allowed(self):
        dest = self.temp_dir / "dest.db"
        dest.write_text("pre-existing")
        src, dst = bdt.resolve_and_guard_destination(
            str(self.source_path), str(dest), True, False, str(self.live_path)
        )
        self.assertEqual(dst, dest.resolve())

    def test_resume_missing_destination_rejected(self):
        dest = self.temp_dir / "does_not_exist.db"
        with self.assertRaises(bdt.ResumeTargetMissingError):
            bdt.resolve_and_guard_destination(
                str(self.source_path), str(dest), False, True, str(self.live_path)
            )

    def test_resume_existing_destination_allowed(self):
        dest = self.temp_dir / "dest.db"
        dest.write_text("pre-existing")
        src, dst = bdt.resolve_and_guard_destination(
            str(self.source_path), str(dest), False, True, str(self.live_path)
        )
        self.assertEqual(dst, dest.resolve())

    def test_overwrite_removes_stale_wal_shm_sidecars_before_fresh_copy(self):
        dest = self.temp_dir / "dest.db"
        dest.write_text("stale destination content")
        wal = self.temp_dir / "dest.db-wal"
        shm = self.temp_dir / "dest.db-shm"
        wal.write_text("stale wal")
        shm.write_text("stale shm")

        bdt.resolve_and_guard_destination(
            str(self.source_path), str(dest), True, False, str(self.live_path)
        )
        bdt.prepare_destination_db(str(self.source_path), str(dest), True)

        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        # Fresh copy landed and is a valid sqlite DB matching the source schema.
        conn = sqlite3.connect(str(dest))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("entities", tables)

    def test_resume_leaves_sidecars_untouched(self):
        dest = self.temp_dir / "dest2.db"
        dest.write_text("existing destination")
        wal = self.temp_dir / "dest2.db-wal"
        shm = self.temp_dir / "dest2.db-shm"
        wal.write_text("live in-progress wal")
        shm.write_text("live in-progress shm")

        # --resume path: guard passes, and (per main()'s branching) prepare_destination_db /
        # _cleanup_wal_shm_sidecars are never called for a resumed run.
        bdt.resolve_and_guard_destination(
            str(self.source_path), str(dest), False, True, str(self.live_path)
        )
        self.assertEqual(wal.read_text(), "live in-progress wal")
        self.assertEqual(shm.read_text(), "live in-progress shm")


class TestOutcomeClassification(unittest.TestCase):
    def test_classify_store_result(self):
        self.assertEqual(
            bdt.classify_store_result("Knowledge stored successfully with ID: abc"),
            ("stored_clean", None),
        )
        # Track A (memory-core rework): the old "[WARNING: Potential duplicate...]" string
        # suffix / "stored_with_duplicate_warning" outcome no longer exists -- store_memory now
        # returns a REVIEW_REQUIRED dict before persistence instead of a warned success string.
        self.assertEqual(
            bdt.classify_store_result({"status": "REVIEW_REQUIRED", "candidates": []})[0],
            "review_required",
        )
        self.assertEqual(
            bdt.classify_store_result({"status": "REVIEW_STALE", "stale_candidate_ids": []})[0],
            "review_stale",
        )
        outcome, detail = bdt.classify_store_result(
            "Error: REJECT_EXACT_DUPLICATE - Memory with exact content hash already exists with ID: x"
        )
        self.assertEqual(outcome, "exact_duplicate_rejected")
        self.assertIsNotNone(detail)
        outcome, _ = bdt.classify_store_result(
            "Error: Memory quality check rejected (Score: 0.10). Reason: too short"
        )
        self.assertEqual(outcome, "quality_rejected")
        outcome, _ = bdt.classify_store_result("Error: something else entirely")
        self.assertEqual(outcome, "other_error")


class TestIngestedEntityShape(unittest.TestCase):
    """Item 7: a stored fixture record has exactly two tags from the fixed vocabulary and a
    metadata mapping matching the Step 4 shape exactly (all 10 keys, nulls where specified)."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.test_data_dir = self.temp_dir / "test_data"
        _write_doc(
            self.test_data_dir / "ag_news",
            "doc_000200_part_000.md",
            {"label": 2, "source_dataset": "ag_news"},
            _PROSE[2],
        )
        _write_doc(
            self.test_data_dir / "squad",
            "doc_000300_part_000.md",
            {"source_dataset": "squad", "title": "Test Article Title"},
            _PROSE[3],
        )
        self.db_path = str(self.temp_dir / "dest.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _entity_tags(self, entity_id):
        rows = self.conn.execute(
            "SELECT t.name FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?",
            (entity_id,),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def test_ag_news_and_squad_entities_have_correct_tags_and_metadata_shape(self):
        manifest = bdt.run_ingestion(
            dest_db_path=self.db_path,
            datasets=["ag_news", "squad"],
            n_per_dataset=None,
            seed="seed",
            test_data_dir=self.test_data_dir,
            checkpoint_every=250,
            run_id="run-shape",
        )
        self.assertEqual(manifest["datasets"]["ag_news"]["stored_clean"], 1)
        self.assertEqual(manifest["datasets"]["squad"]["stored_clean"], 1)

        rows = self.conn.execute(
            "SELECT id, metadata, json_extract(metadata, '$.source_dataset') FROM entities "
            "WHERE owner_id = ?",
            (bdt.OWNER_ID,),
        ).fetchall()
        self.assertEqual(len(rows), 2)

        expected_keys = {
            "source_dataset",
            "source_document_id",
            "original_document_id",
            "part_index",
            "source_relpath",
            "hf_label",
            "source_title",
            "source_url",
            "split_group_id",
            "ingestion_run_id",
        }
        for entity_id, metadata_str, dataset in rows:
            metadata = json.loads(metadata_str)
            self.assertEqual(set(metadata.keys()), expected_keys)

            # resolve_or_create_tag always stores tags '#'-prefixed (normalize_tag_name) --
            # strip that before comparing against TAG_VOCABULARY, which holds bare slugs (the
            # form passed into store_memory's `tags=` list).
            tags = [t.lstrip("#") for t in self._entity_tags(entity_id)]
            self.assertEqual(len(tags), 2)
            self.assertIn("benchmark-corpus", tags)
            self.assertIn(dataset.replace("_", "-"), tags)
            for tag in tags:
                self.assertIn(tag, bdt.TAG_VOCABULARY)

            if dataset == "ag_news":
                self.assertIsNotNone(metadata["hf_label"])
                self.assertIsNone(metadata["source_title"])
                self.assertIsNone(metadata["source_url"])
            elif dataset == "squad":
                self.assertIsNone(metadata["hf_label"])
                self.assertEqual(metadata["source_title"], "Test Article Title")
                self.assertEqual(metadata["split_group_id"], "squad:Test Article Title")


class TestCompletionBarrier(unittest.TestCase):
    """Item 9: a fixture where one entity's embed status is failed, or its chunk row is stale,
    results in corpus_embedding_complete: false."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title, content):
        import re

        res = memory_service.store_memory(
            content=content,
            title=title,
            owner_id=bdt.OWNER_ID,
            skip_duplicate_check=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        return re.search(r"ID:\s*([0-9a-fA-F-]+)", res).group(1)

    def _poll_ready(self, entity_id, tries=50, interval=0.1):
        for _ in range(tries):
            row = self.conn.execute(
                "SELECT embedding_status FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            chunk = self.conn.execute(
                "SELECT 1 FROM entity_chunk_embeddings WHERE entity_id = ? LIMIT 1", (entity_id,)
            ).fetchone()
            if row and row[0] == "ready" and chunk:
                return True
            time.sleep(interval)
        return False

    def test_clean_corpus_reports_complete(self):
        eid = self._store("Completion Barrier Clean Entity", _PROSE[0])
        self.assertTrue(self._poll_ready(eid), "embedding never became ready in time")
        result = bdt.check_embedding_completion(self.conn, bdt.OWNER_ID)
        self.assertTrue(result["corpus_embedding_complete"])

    def test_failed_embedding_status_fails_barrier(self):
        eid = self._store("Completion Barrier Failed Status Entity", _PROSE[1])
        self.assertTrue(self._poll_ready(eid))
        self.conn.execute("UPDATE entities SET embedding_status = 'failed' WHERE id = ?", (eid,))
        self.conn.commit()
        result = bdt.check_embedding_completion(self.conn, bdt.OWNER_ID)
        self.assertFalse(result["corpus_embedding_complete"])
        self.assertFalse(result["entity_level_ready"])

    def test_stale_chunk_row_fails_barrier(self):
        eid = self._store("Completion Barrier Stale Chunk Entity", _PROSE[2])
        self.assertTrue(self._poll_ready(eid))
        # Simulate a content edit that changed content_hash without the async chunk-refresh
        # having landed yet -- the chunk row's content_hash no longer matches.
        self.conn.execute("UPDATE entities SET content_hash = 'stale-hash-xyz' WHERE id = ?", (eid,))
        self.conn.commit()
        result = bdt.check_embedding_completion(self.conn, bdt.OWNER_ID)
        self.assertFalse(result["corpus_embedding_complete"])
        self.assertFalse(result["chunk_level_ready"])
        self.assertGreaterEqual(result["chunk_stale_entities"], 1)


if __name__ == "__main__":
    unittest.main()
