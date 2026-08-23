import io
import json
import os
import shutil
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from unittest.mock import patch

from saltmdb.cli import build_parser, cmd_bootstrap_digest, cmd_corpus_health, cmd_orphans
from saltmdb.db.schema import init_db


class _Args:
    def __init__(self, db_path=None):
        self.db_path = db_path


class TestBootstrapDigestCli(unittest.TestCase):
    """Core-memory bootstrap governance rewrite: cmd_bootstrap_digest is now a thin daemon-call
    wrapper -- rendering itself lives entirely in core_governance_service, which has its own
    dedicated test coverage. This suite only covers the CLI's own responsibilities: db-path
    existence short-circuit, daemon dispatch, and never crashing the caller."""

    def test_missing_db_returns_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "does-not-exist.db")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_bootstrap_digest(_Args(db_path=missing_path))
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")

    def test_prints_daemon_digest_verbatim(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            fake_digest = "<saltmdb-digest>\n\n<core-rules>\n\n</core-rules>\n\n</saltmdb-digest>"
            with patch("saltmdb.daemon.client.call", return_value=fake_digest) as mock_call:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_bootstrap_digest(_Args(db_path=tmp.name))
            self.assertEqual(rc, 0)
            self.assertIn(fake_digest, buf.getvalue())
            mock_call.assert_called_once_with(tmp.name, "get_core_bootstrap_digest", {})

    def test_daemon_failure_never_crashes_caller(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch("saltmdb.daemon.client.call", side_effect=RuntimeError("boom")):
                buf = io.StringIO()
                err = io.StringIO()
                from contextlib import redirect_stderr

                with redirect_stdout(buf), redirect_stderr(err):
                    rc = cmd_bootstrap_digest(_Args(db_path=tmp.name))
            self.assertEqual(rc, 0)
            self.assertIn("Warning", err.getvalue())

    def test_non_string_daemon_response_falls_back_to_empty_digest(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch("saltmdb.daemon.client.call", return_value=None):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_bootstrap_digest(_Args(db_path=tmp.name))
            self.assertEqual(rc, 0)
            self.assertIn("<saltmdb-digest>", buf.getvalue())


class TestBuildParser(unittest.TestCase):
    def test_bootstrap_digest_subcommand_has_no_legacy_flags(self):
        parser = build_parser()
        args = parser.parse_args(["bootstrap-digest"])
        self.assertEqual(args.command, "bootstrap-digest")
        # The old project-keywords/core-limit/project-limit/no-semantic machinery is retired --
        # simplified hooks/CLI invoke this with no extra flags at all.
        for legacy_attr in ("project_keywords", "core_limit", "project_limit", "no_semantic"):
            self.assertFalse(hasattr(args, legacy_attr), f"unexpected legacy attr: {legacy_attr}")

    def test_db_path_override(self):
        parser = build_parser()
        args = parser.parse_args(["--db-path", "/tmp/custom.db", "bootstrap-digest"])
        self.assertEqual(args.db_path, "/tmp/custom.db")

    def test_export_corpus_snapshot_subcommand_requires_owner_id(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["export-corpus-snapshot"])

    def test_export_corpus_snapshot_subcommand_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["export-corpus-snapshot", "--owner-id", "alice"])
        self.assertEqual(args.owner_id, "alice")
        self.assertIsNone(args.page_size)
        self.assertFalse(args.include_archived)
        self.assertIsNone(args.out)

    def test_orphans_subcommand_owner_id_optional(self):
        parser = build_parser()
        args = parser.parse_args(["orphans"])
        self.assertIsNone(args.owner_id)

    def test_corpus_health_subcommand_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["corpus-health"])
        self.assertEqual(args.days, 7)
        self.assertEqual(args.telemetry_limit, 10)


class _OrphansArgs:
    def __init__(self, db_path=None, owner_id=None):
        self.db_path = db_path
        self.owner_id = owner_id


class TestOrphansCli(unittest.TestCase):
    """Phase 7 item 30: orphan detection moved off MCP -- cmd_orphans is a thin wrapper around
    the pre-existing memory_service.detect_orphaned_memories, unit-tested on its own already;
    this only covers the CLI's own responsibilities (owner_id passthrough, JSON output, exit
    code on error)."""

    def test_passes_owner_id_and_prints_json(self):
        fake_result = {"total_orphans": 1, "orphaned_memories": [{"id": "e1"}]}
        with patch(
            "saltmdb.domain.services.memory_service.detect_orphaned_memories",
            return_value=fake_result,
        ) as mock_detect:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_orphans(_OrphansArgs(db_path="/tmp/whatever.db", owner_id="alice"))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), fake_result)
        mock_detect.assert_called_once_with(owner_id="alice", db_path="/tmp/whatever.db")

    def test_error_result_returns_nonzero_exit(self):
        with patch(
            "saltmdb.domain.services.memory_service.detect_orphaned_memories",
            return_value={"error": "boom"},
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_orphans(_OrphansArgs())
        self.assertEqual(rc, 1)


class _CorpusHealthArgs:
    def __init__(self, db_path=None, days=7, telemetry_limit=10):
        self.db_path = db_path
        self.days = days
        self.telemetry_limit = telemetry_limit


class TestCorpusHealthCli(unittest.TestCase):
    """Phase 7 item 31: the corpus-health report, CLI-only per plan §5.10 (never an MCP tool).
    Integration-tested against a real init_db()'d temp DB, not mocked -- every signal here is a
    plain SQL aggregate or a call into an existing, separately-unit-tested domain function, so
    the value of this suite is verifying the real queries against the real schema."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_corpus_health(_CorpusHealthArgs(db_path=self.db_path))
        return rc, json.loads(buf.getvalue())

    def test_missing_db_reports_nothing_without_error(self):
        missing = os.path.join(self.temp_dir, "does-not-exist.db")
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = cmd_corpus_health(_CorpusHealthArgs(db_path=missing))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_empty_db_reports_all_zeros(self):
        rc, report = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(report["entities"]["total"], 0)
        self.assertEqual(report["flagged_stale"]["count"], 0)
        self.assertEqual(report["orphaned_memories"]["total_orphans"], 0)
        self.assertEqual(report["overdue_core_reviews"]["count"], 0)
        self.assertEqual(report["predicate_drift"]["drifted_active_edge_count"], 0)
        # init_db() seeds 3 canonical top-level tags (episodic/semantic/procedural) on every
        # fresh DB (schema.py) -- "empty" means no fragmentation, not zero tags.
        self.assertEqual(report["tag_fragmentation"]["total_tags"], 3)
        self.assertEqual(report["tag_fragmentation"]["canonical_tags"], 3)
        self.assertEqual(report["tag_fragmentation"]["alias_tags"], 0)
        self.assertEqual(report["telemetry"]["total_calls"], 0)

    def _mk_entity(
        self, title, *, is_core=False, core_review_after=None, status="raw", metadata=None
    ):
        entity_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        metadata_str = json.dumps(metadata) if metadata else None
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "scope, is_core, status, title, full_content, valid_from, core_reason, "
            "core_exit_condition, core_review_after, metadata) VALUES "
            "(?, ?, ?, ?, 'tester', 'shared', ?, ?, ?, 'body content here', ?, ?, ?, ?, ?)",
            (
                entity_id,
                now,
                now,
                now,
                1 if is_core else 0,
                status,
                title,
                now,
                "reason " * 5 if is_core else None,
                "exit " * 5 if is_core else None,
                core_review_after,
                metadata_str,
            ),
        )
        return entity_id

    def test_flagged_stale_count_includes_active_excludes_archived(self):
        self._mk_entity(
            "Active Flagged",
            metadata={"drift_flag": {"reason": "changed"}},
            status="raw",
        )
        self._mk_entity(
            "Archived Flagged",
            metadata={"drift_flag": {"reason": "changed"}},
            status="archived",
        )
        self.conn.commit()

        rc, report = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(report["flagged_stale"]["count"], 1)

    def test_core_count_excludes_archived_former_cores(self):
        self._mk_entity(
            "Archived Former Core",
            is_core=True,
            core_review_after="2020-01-01T00:00:00+00:00",
            status="archived",
        )
        self._mk_entity(
            "Active Core",
            is_core=True,
            core_review_after="2099-01-01T00:00:00+00:00",
        )
        self.conn.commit()

        rc, report = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(report["entities"]["core"], 1)

    def test_reports_orphans_overdue_cores_and_predicate_drift(self):
        orphan_id = self._mk_entity("Orphan")
        overdue_core_id = self._mk_entity(
            "Overdue Core", is_core=True, core_review_after="2020-01-01T00:00:00+00:00"
        )
        a = self._mk_entity("Related A")
        b = self._mk_entity("Related B")
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, "
            "valid_from, valid_at) VALUES (?, ?, ?, 'relates_to', ?, ?, ?)",
            (str(uuid.uuid4()), a, b, now, now, now),
        )
        self.conn.commit()

        rc, report = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(report["entities"]["raw"], 4)
        self.assertEqual(report["entities"]["core"], 1)

        orphan_ids = {o["id"] for o in report["orphaned_memories"]["orphaned_memories"]}
        self.assertEqual(orphan_ids, {orphan_id, overdue_core_id})

        self.assertEqual(report["overdue_core_reviews"]["count"], 1)
        self.assertEqual(report["overdue_core_reviews"]["entries"][0]["id"], overdue_core_id)

        self.assertEqual(report["predicate_drift"]["by_predicate"], {"relates_to": 1})
        self.assertEqual(report["predicate_drift"]["drifted_active_edge_count"], 1)

    def test_days_and_telemetry_limit_are_respected(self):
        for i in range(3):
            self.conn.execute(
                "INSERT INTO tool_call_telemetry (id, timestamp, tool_name, param_names, "
                "status, error_code, latency_ms) VALUES (?, ?, 'store_memory', '[]', "
                "'rejected', ?, 5.0)",
                (str(uuid.uuid4()), datetime.now(UTC).isoformat(), f"CODE_{i}"),
            )
        self.conn.commit()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_corpus_health(
                _CorpusHealthArgs(db_path=self.db_path, days=7, telemetry_limit=2)
            )
        report = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(report["telemetry"]["total_calls"], 3)
        self.assertEqual(len(report["telemetry"]["top_error_codes"]), 2)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cmd_corpus_health(_CorpusHealthArgs(db_path=self.db_path, days=0, telemetry_limit=10))
        report2 = json.loads(buf2.getvalue())
        self.assertEqual(
            report2["telemetry"]["total_calls"], 0, "days=0 must exclude rows just inserted 'now'"
        )


if __name__ == "__main__":
    unittest.main()
