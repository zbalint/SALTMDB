"""SPEC-RELATED-MEMORIES-REPLAY (BL-028): the offline replay harness, on temp databases only.

The cross-encoder and the query embedding are patched; T3 runs the real vec0 nearest-neighbour
step with small fixed vectors so the rewind is proven against the production semantic channel.
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import sqlite_vec

from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service, reranker_service
from saltmdb.domain.services.memory_service import ranking

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "benchmarking" / "replay_related_memories.py"
_spec = importlib.util.spec_from_file_location("replay_related_memories", _MODULE_PATH)
rrm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rrm)

DIM = 384
LONG_REPLY = (
    "The orchard pruning schedule was settled earlier and the irrigation plan builds on it "
    "directly, so the summer watering rota keeps the same rows and the same order as before."
)


def _vector(x: float, y: float) -> bytes:
    values = [0.0] * DIM
    values[0], values[1] = x, y
    return sqlite_vec.serialize_float32(values)


def _query_vector(_text):
    values = [0.0] * DIM
    values[0] = 1.0
    return values


class _Db:
    """A temp database with the project schema and small helpers for rows."""

    def __init__(self, path: str):
        self.path = path
        self.conn = init_db(path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)

    def entity(self, entity_id, created_at, *, title=None, content=None, status="raw", vec=None):
        self.conn.execute(
            """INSERT INTO entities
            (id, title, full_content, scope, agent_id, status, created_at, updated_at,
             last_accessed_at, valid_from, embedding_status)
            VALUES (?, ?, ?, 'shared', 'agent_a', ?, ?, ?, ?, ?, 'ready')""",
            (
                entity_id,
                title or entity_id,
                content or f"content of {entity_id}",
                status,
                created_at,
                created_at,
                created_at,
                created_at,
            ),
        )
        if vec is not None:
            self.conn.execute(
                "INSERT INTO entity_embeddings(entity_id, embedding) VALUES (?, ?)",
                (entity_id, _vector(*vec)),
            )
        self.conn.commit()

    def relation(self, rel_id, source, target, created_at, *, predicate="supersedes", **cols):
        self.conn.execute(
            """INSERT INTO relations
            (id, source_id, target_id, predicate, created_at, valid_from, valid_to, valid_at,
             invalid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel_id,
                source,
                target,
                predicate,
                created_at,
                created_at,
                cols.get("valid_to"),
                cols.get("valid_at"),
                cols.get("invalid_at"),
            ),
        )
        self.conn.commit()

    def trace(
        self,
        trace_id,
        message,
        *,
        status="completed",
        created_at,
        completed_at=None,
        msg_hash=None,
        agent_id="agent_a",
    ):
        self.conn.execute(
            """INSERT INTO conversation_traces
            (id, agent_session_id, agent_id, harness, harness_session_id, harness_turn_id,
             status, user_prompt, user_prompt_hash, final_assistant_message,
             final_assistant_message_hash, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, 'claude_code', 'hs', ?, ?, 'prompt', 'ph', ?, ?, ?, ?, ?)""",
            (
                trace_id,
                f"session-{trace_id}",
                agent_id,
                trace_id,
                status,
                message,
                msg_hash or f"hash-{trace_id}",
                created_at,
                created_at,
                completed_at,
            ),
        )
        self.conn.commit()

    def has_vector(self, entity_id) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM entity_embeddings WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        return row[0] == 1

    def status(self, entity_id) -> str:
        return self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()[0]


class _TempDbCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = directory.name
        self.db = _Db(os.path.join(self.tmp, "work.db"))
        self.addCleanup(self.db.conn.close)


class TestSelectReplies(_TempDbCase):
    """T1."""

    def _seed(self):
        long_text = "x" * 250
        self.db.trace("t01", long_text + "1", created_at="2026-10-01 10:00:00")
        self.db.trace(
            "t02",
            long_text + "2",
            created_at="2026-10-01T09:00:00+00:00",
            completed_at="2026-10-05T09:00:00+00:00",
        )
        self.db.trace("t03", long_text + "3", created_at="2026-10-03 10:00:00")
        self.db.trace("t04", long_text + "4", created_at="2026-10-04 10:00:00", status="incomplete")
        self.db.trace("t05", "too short", created_at="2026-10-06 10:00:00")
        self.db.trace("t06", long_text + "6", created_at="2026-10-02 10:00:00", msg_hash="hash-t01")
        self.db.trace("t07", long_text + "7", created_at="2026-10-07 10:00:00")
        self.db.trace("t08", long_text + "8", created_at="2026-10-08 10:00:00")

    def test_filters_dedups_and_orders_newest_first(self):
        self._seed()
        replies = rrm.select_replies(self.db.conn, 100, 1, 200, [])
        ids = [reply["trace_id"] for reply in replies]
        self.assertEqual(ids, ["t08", "t07", "t02", "t03", "t01"])
        self.assertNotIn("t04", ids)  # incomplete
        self.assertNotIn("t05", ids)  # short
        self.assertNotIn("t06", ids)  # duplicate hash of t01
        by_id = {reply["trace_id"]: reply for reply in replies}
        self.assertEqual(by_id["t02"]["reply_time"], "2026-10-05T09:00:00+00:00")
        self.assertEqual(by_id["t01"]["reply_time"], "2026-10-01 10:00:00")
        self.assertEqual(by_id["t01"]["agent_id"], "agent_a")

    def test_forced_traces_first_and_same_seed_same_sample(self):
        self._seed()
        first = rrm.select_replies(self.db.conn, 2, 7, 200, ["t03"])
        second = rrm.select_replies(self.db.conn, 2, 7, 200, ["t03"])
        self.assertEqual(first, second)
        ids = [reply["trace_id"] for reply in first]
        self.assertEqual(len(ids), 2)
        self.assertIn("t03", ids)
        self.assertTrue(set(ids) <= {"t01", "t02", "t03", "t07", "t08"})
        many = {
            tuple(r["trace_id"] for r in rrm.select_replies(self.db.conn, 2, seed, 200, []))
            for seed in range(20)
        }
        self.assertGreater(len(many), 1, "different seeds should give different samples")


class TestRewindCopy(_TempDbCase):
    """T2."""

    @staticmethod
    def _seed(db):
        db.entity("old", "2026-10-10 11:00:00", vec=(1.0, 0.0))
        db.entity("old2", "2026-10-10T11:30:00+00:00", vec=(0.9, 0.1))
        db.entity("new", "2026-10-10T13:00:00+00:00", vec=(0.8, 0.2))
        db.entity("new_space", "2026-10-10 12:30:00")
        db.relation("r_from_new", "new", "old", "2026-10-10 13:00:00", predicate="related_to")
        # Created after the cutoff between two earlier entities: old2 must not supersede old.
        db.relation("r_late", "old2", "old", "2026-10-10T12:15:00+00:00")
        # Valid at the cutoff, expired later (but before now): kept, reopened.
        db.relation(
            "r_expired_later",
            "old",
            "old2",
            "2026-10-10 11:45:00",
            predicate="related_to",
            valid_to="2026-10-10T14:00:00+00:00",
            invalid_at="2026-10-10 14:00:00",
        )
        # Expired before the cutoff: untouched.
        db.relation(
            "r_expired_before",
            "old2",
            "old",
            "2026-10-10 11:40:00",
            predicate="depends_on",
            valid_to="2026-10-10 11:50:00",
        )

    @staticmethod
    def _relation_ids(db):
        return {row[0] for row in db.conn.execute("SELECT id FROM relations")}

    def test_rewind_hides_later_state_in_both_timestamp_forms(self):
        for index, cutoff in enumerate(("2026-10-10 12:00:00", "2026-10-10T12:00:00+00:00")):
            with self.subTest(cutoff=cutoff):
                db = _Db(os.path.join(self.tmp, f"rewind-{index}.db"))
                self.addCleanup(db.conn.close)
                self._seed(db)
                rrm.rewind_copy(db.conn, cutoff)

                self.assertEqual(db.status("new"), "archived")
                self.assertEqual(db.status("new_space"), "archived")
                self.assertFalse(db.has_vector("new"))
                self.assertEqual(db.status("old"), "raw")
                self.assertEqual(db.status("old2"), "raw")
                self.assertTrue(db.has_vector("old"))
                self.assertTrue(db.has_vector("old2"))

                relations = self._relation_ids(db)
                self.assertNotIn("r_from_new", relations)
                self.assertNotIn("r_late", relations)
                self.assertIn("r_expired_later", relations)
                self.assertIn("r_expired_before", relations)
                self.assertEqual(
                    ranking._compute_superseded_ids_bitemporal(["old", "old2"], db.conn), set()
                )
                valid_to, invalid_at = db.conn.execute(
                    "SELECT valid_to, invalid_at FROM relations WHERE id = 'r_expired_later'"
                ).fetchone()
                self.assertIsNone(valid_to)
                self.assertIsNone(invalid_at)
                kept = db.conn.execute(
                    "SELECT valid_to FROM relations WHERE id = 'r_expired_before'"
                ).fetchone()[0]
                self.assertEqual(kept, "2026-10-10 11:50:00")

    def test_second_earlier_cutoff_only_rewinds_more(self):
        self._seed(self.db)
        rrm.rewind_copy(self.db.conn, "2026-10-10 12:00:00")
        rrm.rewind_copy(self.db.conn, "2026-10-10T11:15:00+00:00")
        self.assertEqual(self.db.status("old"), "raw")
        self.assertEqual(self.db.status("old2"), "archived")
        self.assertEqual(self.db.status("new"), "archived")
        self.assertFalse(self.db.has_vector("old2"))
        self.assertTrue(self.db.has_vector("old"))
        self.assertEqual(self._relation_ids(self.db), set())


class TestSemanticPoolAfterRewind(_TempDbCase):
    """T3: real vec0 nearest-neighbour step, fixed vectors, patched query embedding and CE."""

    def setUp(self):
        super().setUp()
        model = Mock()
        model.rerank.side_effect = lambda _segment, texts: [5.0 for _ in texts]
        for target in (
            patch.object(reranker_service, "get_model", return_value=model),
            patch.object(embedding_service, "embed_query_text", side_effect=_query_vector),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_rewound_neighbours_free_their_slots_for_older_candidates(self):
        # Twelve later memories sit closest to the query: more than the 10-row semantic request.
        for i in range(12):
            self.db.entity(
                f"late{i:02d}",
                "2026-10-10T13:00:00+00:00",
                title=f"orchard watering rota late {i}",
                vec=(1.0, 0.001 * i),
            )
        older = [f"early{i}" for i in range(4)]
        for i, entity_id in enumerate(older):
            self.db.entity(
                entity_id,
                "2026-10-09 10:00:00",
                title=f"unrelated wording {i}",
                vec=(0.6, 0.4 + 0.01 * i),
            )

        rrm.rewind_copy(self.db.conn, "2026-10-10 12:00:00")
        rows = rrm.related_memory_service.find_related_memories(
            self.db.conn, self.db.path, LONG_REPLY, agent_id="agent_a", with_all=True
        )

        ids = {row["id"] for row in rows}
        self.assertEqual(ids, set(older))
        semantic_ids = {row["id"] for row in rows if row["semantic_distance"] is not None}
        self.assertEqual(semantic_ids, set(older))
        self.assertFalse(any(row["id"].startswith("late") for row in rows))

    def test_without_vector_cleanup_the_pool_would_be_starved(self):
        """Control for T3: archiving alone leaves the later rows in the neighbour slots."""
        for i in range(12):
            self.db.entity(f"late{i:02d}", "2026-10-10T13:00:00+00:00", vec=(1.0, 0.001 * i))
        self.db.entity("early", "2026-10-09 10:00:00", title="unrelated", vec=(0.6, 0.4))
        with patch.object(rrm.embedding_service, "clear_embedding_vectors_for_entity"):
            rrm.rewind_copy(self.db.conn, "2026-10-10 12:00:00")
        rows = rrm.related_memory_service.find_related_memories(
            self.db.conn, self.db.path, LONG_REPLY, agent_id="agent_a", with_all=True
        )
        self.assertEqual(rows, [])


def _make_snapshot(path: str) -> None:
    db = _Db(path)
    db.entity("m1", "2026-10-01 10:00:00", content="c" * 900)
    db.entity("m2", "2026-10-02 10:00:00")
    db.entity("m3", "2026-10-03 10:00:00")
    db.trace("t1", "A" * 1000 + "B" * 1000, created_at="2026-10-05 10:00:00")
    db.trace("t2", "reply two " * 30, created_at="2026-10-06 10:00:00")
    db.trace("t3", "reply three " * 30, created_at="2026-10-07 10:00:00")
    db.conn.execute("PRAGMA journal_mode=DELETE")
    db.conn.close()


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class _PrepareCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = directory.name
        self.snapshot = os.path.join(self.tmp, "snap", "saltmdb_snapshot_20261010_120000.db")
        os.makedirs(os.path.dirname(self.snapshot))
        _make_snapshot(self.snapshot)
        self.out = os.path.join(self.tmp, "out")
        live = patch.dict(os.environ, {"SALTMDB_DB_PATH": os.path.join(self.tmp, "live.db")})
        live.start()
        self.addCleanup(live.stop)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rrm.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestGuard(_PrepareCase):
    """T4."""

    def _refused(self, snapshot, out_dir):
        before = sorted(os.listdir(self.tmp))
        code, _out, err = self.run_main(["prepare", "--snapshot", snapshot, "--out-dir", out_dir])
        self.assertEqual(code, 2, err)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)
        return err

    def test_live_snapshot_path_is_refused(self):
        with patch.dict(os.environ, {"SALTMDB_DB_PATH": self.snapshot}):
            self._refused(self.snapshot, self.out)

    def test_live_work_db_path_is_refused(self):
        work = os.path.join(self.out, "work.db")
        with patch.dict(os.environ, {"SALTMDB_DB_PATH": work}):
            self._refused(self.snapshot, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_snapshot_inside_repository_is_refused(self):
        inside = str(_REPO_ROOT / "scratch-bl028-never-created" / "snap.db")
        self._refused(inside, self.out)

    def test_output_inside_repository_outside_scratch_is_refused(self):
        unsafe = _REPO_ROOT / "docs" / "bl028-never-created"
        self._refused(self.snapshot, str(unsafe))
        self.assertFalse(unsafe.exists())

    def test_existing_work_db_is_refused(self):
        os.makedirs(self.out)
        work = os.path.join(self.out, "work.db")
        with open(work, "wb") as f:
            f.write(b"existing")
        self._refused(self.snapshot, self.out)
        self.assertEqual(_read(work), b"existing")
        self.assertEqual(os.listdir(self.out), ["work.db"])

    def test_output_equal_to_repository_root_is_refused(self):
        """Review round 1 fix 4: the repo root itself has no first path part."""
        self._refused(self.snapshot, str(_REPO_ROOT))
        self.assertFalse((_REPO_ROOT / "work.db").exists())

    def test_output_under_scratch_passes_the_guard(self):
        allowed = str(_REPO_ROOT / "scratch-bl028-never-created" / "out")
        rrm._guard(self.snapshot, allowed)  # must not raise; nothing is created by the guard
        self.assertFalse(os.path.exists(allowed))


class TestPrepareEndToEnd(_PrepareCase):
    """T5 (and T4's byte-identical snapshot on success)."""

    def _fake_find(self, _conn, _db_path, text, **kwargs):
        self.calls.append((text[:5], kwargs))
        return [
            {
                "id": "m2",
                "title": "m2",
                "score": 5.0,
                "segment": 0,
                "fts_rank": 1,
                "semantic_distance": None,
            },
            {
                "id": "m3",
                "title": "m3",
                "score": 5.0,
                "segment": 1,
                "fts_rank": None,
                "semantic_distance": 0.2,
            },
            {
                "id": "m1",
                "title": "m1",
                "score": 7.5,
                "segment": 0,
                "fts_rank": 2,
                "semantic_distance": 0.1,
            },
        ]

    def test_prepare_writes_candidates_packets_manifest_and_instructions(self):
        self.calls = []
        before = _read(self.snapshot)
        with patch.object(
            rrm.related_memory_service, "find_related_memories", side_effect=self._fake_find
        ):
            code, _out, err = self.run_main(
                [
                    "prepare",
                    "--snapshot",
                    self.snapshot,
                    "--out-dir",
                    self.out,
                    "--min-chars",
                    "200",
                    "--limit-candidates",
                    "2",
                ]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(_read(self.snapshot), before)
        self.assertTrue(
            all(kwargs == {"agent_id": "agent_a", "with_all": True} for _, kwargs in self.calls)
        )

        rows = _jsonl(os.path.join(self.out, "candidates.jsonl"))
        self.assertEqual([row["trace_id"] for row in rows], ["t3", "t2", "t1"])
        for row in rows:
            self.assertEqual(
                set(row),
                {"trace_id", "reply_time", "agent_id", "latency_ms", "segments", "candidates"},
            )
            self.assertEqual([c["id"] for c in row["candidates"]], ["m1", "m2"])
            self.assertEqual(
                set(row["candidates"][0]),
                {"id", "score", "segment", "fts_rank", "semantic_distance"},
            )
            # Review round 1 fix 1: D2.7's `segments` is a count; each reply is one paragraph.
            self.assertIs(type(row["segments"]), int)
            self.assertEqual(row["segments"], 1)

        packets = _jsonl(os.path.join(self.out, "packets", "001.jsonl"))
        self.assertEqual(len(packets), 3)
        t1 = next(p for p in packets if p["trace_id"] == "t1")
        self.assertEqual(t1["reply_tail"], "A" * 500 + "B" * 1000)
        m1 = next(c for c in t1["candidates"] if c["id"] == "m1")
        self.assertEqual(m1["content"], "c" * 600)
        self.assertEqual(m1["title"], "m1")

        with open(os.path.join(self.out, "MANIFEST.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["counts"], {"replies": 3, "with_candidates": 3, "without": 0})
        self.assertEqual(manifest["snapshot"]["name"], os.path.basename(self.snapshot))
        self.assertEqual(manifest["snapshot"]["bytes"], len(before))
        self.assertEqual(manifest["seed"], 1)
        self.assertEqual(manifest["sample"], 200)
        self.assertEqual(manifest["reply_time_min"], "2026-10-05 10:00:00")
        self.assertEqual(manifest["reply_time_max"], "2026-10-07 10:00:00")
        self.assertEqual(manifest["forced_dropped"], [])
        # Review round 1 fix 5: memories written during the reply's own turn stay visible.
        self.assertTrue(any("completed_at" in line for line in manifest["limitations"]))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "JUDGE_INSTRUCTIONS.txt")))

    def test_forced_traces_not_selected_are_reported(self):
        """Review round 1 fix 3: dropped --include-traces ids go to stderr and the manifest."""
        include = os.path.join(self.tmp, "include.jsonl")
        with open(include, "w", encoding="utf-8") as f:
            f.write(json.dumps({"trace_id": "t1"}) + "\n")
            f.write(json.dumps({"trace_id": "t_missing"}) + "\n")
        with patch.object(rrm.related_memory_service, "find_related_memories", return_value=[]):
            code, _out, err = self.run_main(
                [
                    "prepare",
                    "--snapshot",
                    self.snapshot,
                    "--out-dir",
                    self.out,
                    "--sample",
                    "1",
                    "--include-traces",
                    include,
                ]
            )
        self.assertEqual(code, 0, err)
        self.assertIn("t_missing", err)
        with open(os.path.join(self.out, "MANIFEST.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["forced_dropped"], ["t_missing"])
        rows = _jsonl(os.path.join(self.out, "candidates.jsonl"))
        self.assertEqual([row["trace_id"] for row in rows], ["t1"])


def _cand(memory_id, score):
    return {
        "id": memory_id,
        "score": score,
        "segment": 0,
        "fts_rank": None,
        "semantic_distance": None,
    }


class TestAnalyzeLabels(unittest.TestCase):
    """T6: 4 replies, 3 thresholds, limit 2, literal expectations."""

    ROWS = [
        {
            "trace_id": "r1",
            "latency_ms": 900.0,
            "candidates": [_cand("a", 7.0), _cand("b", 5.0), _cand("c", 3.0)],
        },
        {
            "trace_id": "r2",
            "latency_ms": 100.0,
            "candidates": [_cand("d", 6.0), _cand("e", 6.0), _cand("f", 6.0)],
        },
        {"trace_id": "r3", "latency_ms": 200.0, "candidates": [_cand("g", 2.5)]},
        {"trace_id": "r4", "latency_ms": 300.0, "candidates": []},
    ]
    LABELS = {
        ("r1", "a"): "relevant",
        ("r1", "b"): "irrelevant",
        ("r1", "c"): "partly",
        ("r2", "d"): "irrelevant",
        ("r2", "e"): "partly",
        ("r2", "f"): "relevant",
        ("r3", "g"): "irrelevant",
    }

    def test_worked_example(self):
        result = rrm.analyze_labels(
            self.ROWS, self.LABELS, {("r1", "a"), ("r2", "f")}, [2.0, 5.0, 7.0], 2
        )
        by_t = {entry["threshold"]: entry for entry in result["thresholds"]}

        # t=2: r1 shows a,b; r2 shows d,e (f cut by the limit: stored order decides); r3 shows g.
        self.assertEqual(
            by_t[2.0]["counts"],
            {"replies": 4, "blocked": 3, "shown": 5, "relevant": 1, "partly": 1, "irrelevant": 3},
        )
        self.assertAlmostEqual(by_t[2.0]["block_rate"], 0.75)
        self.assertAlmostEqual(by_t[2.0]["precision"], 0.3)
        self.assertAlmostEqual(by_t[2.0]["false_block_rate"], 0.5)
        self.assertEqual((by_t[2.0]["known_hits"], by_t[2.0]["known_total"]), (1, 2))

        # t=5: r1 shows a,b; r2 shows d,e; r3 nothing; r4 nothing (empty set, not a false block).
        self.assertEqual(by_t[5.0]["counts"]["blocked"], 2)
        self.assertAlmostEqual(by_t[5.0]["block_rate"], 0.5)
        self.assertAlmostEqual(by_t[5.0]["precision"], 1.5 / 4)
        self.assertAlmostEqual(by_t[5.0]["false_block_rate"], 0.25)

        # t=7: only r1 shows a.
        self.assertAlmostEqual(by_t[7.0]["block_rate"], 0.25)
        self.assertAlmostEqual(by_t[7.0]["precision"], 1.0)
        self.assertAlmostEqual(by_t[7.0]["false_block_rate"], 0.0)
        self.assertEqual((by_t[7.0]["known_hits"], by_t[7.0]["known_total"]), (1, 2))

    def test_unlabelled_candidate_raises_with_count(self):
        labels = dict(self.LABELS)
        del labels[("r1", "c")]
        del labels[("r3", "g")]
        with self.assertRaisesRegex(ValueError, "2 unlabelled"):
            rrm.analyze_labels(self.ROWS, labels, set(), [2.0], 3)


class TestAnalyzeCommand(unittest.TestCase):
    """T7."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name

    def _write(self, name, rows):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rrm.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_unlabelled_candidates_exit_two_with_count(self):
        self._write("candidates.jsonl", TestAnalyzeLabels.ROWS)
        labels = self._write(
            "labels.jsonl", [{"trace_id": "r1", "memory_id": "a", "label": "relevant"}]
        )
        code, _out, err = self._run(["analyze", "--dir", self.dir, "--labels", labels])
        self.assertEqual(code, 2)
        self.assertIn("6 unlabelled", err)

    def test_zero_replies_exit_two(self):
        self._write("candidates.jsonl", [])
        labels = self._write("labels.jsonl", [])
        code, _out, err = self._run(["analyze", "--dir", self.dir, "--labels", labels])
        self.assertEqual(code, 2)
        self.assertIn("no replies", err)

    def test_nothing_meets_the_bar_and_null_precision_never_passes(self):
        rows = [
            {"trace_id": "r1", "latency_ms": 10.0, "candidates": [_cand("a", 3.0)]},
            {"trace_id": "r2", "latency_ms": 10.0, "candidates": []},
        ]
        self._write("candidates.jsonl", rows)
        labels = self._write(
            "labels.jsonl", [{"trace_id": "r1", "memory_id": "a", "label": "irrelevant"}]
        )
        code, out, _err = self._run(
            ["analyze", "--dir", self.dir, "--labels", labels, "--thresholds", "2,8"]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        by_t = {entry["threshold"]: entry for entry in report["thresholds"]}
        self.assertIsNone(by_t[8.0]["precision"])
        self.assertAlmostEqual(by_t[8.0]["block_rate"], 0.0)
        self.assertFalse(by_t[8.0]["meets_bar"])
        self.assertFalse(by_t[2.0]["meets_bar"])
        self.assertEqual(report["lowest_passing_threshold"], "none")
        with open(os.path.join(self.dir, "analysis.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), report)

    def test_lowest_passing_threshold_is_named(self):
        rows = [{"trace_id": f"r{i}", "latency_ms": 10.0, "candidates": []} for i in range(9)]
        rows.append({"trace_id": "hit", "latency_ms": 10.0, "candidates": [_cand("a", 6.0)]})
        self._write("candidates.jsonl", rows)
        labels = self._write(
            "labels.jsonl", [{"trace_id": "hit", "memory_id": "a", "label": "relevant"}]
        )
        known = self._write("known.jsonl", [{"trace_id": "hit", "memory_id": "a"}])
        code, out, _err = self._run(
            [
                "analyze",
                "--dir",
                self.dir,
                "--labels",
                labels,
                "--known-positives",
                known,
                "--thresholds",
                "7,5,6",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["lowest_passing_threshold"], 5.0)

    def test_unknown_label_value_exits_two_naming_count_and_sample(self):
        """Review round 1 fix 2: a label outside the three values is an error, not a KeyError."""
        self._write(
            "candidates.jsonl",
            [{"trace_id": "r1", "latency_ms": 1.0, "candidates": [_cand("a", 6.0)]}],
        )
        labels = self._write(
            "labels.jsonl", [{"trace_id": "r1", "memory_id": "a", "label": "Relevant"}]
        )
        code, _out, err = self._run(["analyze", "--dir", self.dir, "--labels", labels])
        self.assertEqual(code, 2)
        self.assertIn("# Error: 1 label", err)
        self.assertIn("'Relevant'", err)

    def test_conflicting_duplicate_labels_exit_two(self):
        self._write(
            "candidates.jsonl",
            [{"trace_id": "r1", "latency_ms": 1.0, "candidates": [_cand("a", 6.0)]}],
        )
        labels = self._write(
            "labels.jsonl",
            [
                {"trace_id": "r1", "memory_id": "a", "label": "relevant"},
                {"trace_id": "r1", "memory_id": "a", "label": "relevant"},
                {"trace_id": "r1", "memory_id": "a", "label": "irrelevant"},
            ],
        )
        code, _out, err = self._run(["analyze", "--dir", self.dir, "--labels", labels])
        self.assertEqual(code, 2)
        self.assertIn("# Error: 1 (trace_id, memory_id) pair", err)

    def test_known_pairs_outside_the_sample_are_reported(self):
        """Review round 1 fix 3: known_unsampled explains a failing known_hits; rule unchanged."""
        self._write(
            "candidates.jsonl",
            [{"trace_id": "r1", "latency_ms": 1.0, "candidates": [_cand("a", 6.0)]}],
        )
        labels = self._write(
            "labels.jsonl", [{"trace_id": "r1", "memory_id": "a", "label": "relevant"}]
        )
        known = self._write(
            "known.jsonl",
            [{"trace_id": "r1", "memory_id": "a"}, {"trace_id": "r_gone", "memory_id": "z"}],
        )
        code, out, _err = self._run(
            [
                "analyze",
                "--dir",
                self.dir,
                "--labels",
                labels,
                "--known-positives",
                known,
                "--thresholds",
                "5",
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["known_unsampled"], [["r_gone", "z"]])
        entry = report["thresholds"][0]
        self.assertEqual((entry["known_hits"], entry["known_total"]), (1, 2))
        self.assertFalse(entry["meets_bar"])


class TestLatency(unittest.TestCase):
    """T8."""

    def test_nearest_rank_excludes_the_first_value(self):
        summary = rrm.latency_summary([1000.0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        self.assertEqual(summary["first_ms"], 1000.0)
        self.assertEqual(summary["count"], 10)
        self.assertEqual(summary["p50_ms"], 50)
        self.assertEqual(summary["p95_ms"], 100)

    def test_single_value_has_no_percentiles(self):
        summary = rrm.latency_summary([5.0])
        self.assertEqual(summary["first_ms"], 5.0)
        self.assertIsNone(summary["p50_ms"])
        self.assertIsNone(summary["p95_ms"])


if __name__ == "__main__":
    unittest.main()
