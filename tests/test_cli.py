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

from saltmdb.cli import (
    build_parser,
    cmd_bootstrap_digest,
    cmd_corpus_health,
    cmd_orphans,
    cmd_session_digest,
)
from saltmdb.db.schema import init_db


class _Args:
    def __init__(self, db_path=None, max_chars=None, before=None):
        self.db_path = db_path
        self.max_chars = max_chars
        self.before = before


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
            self.assertIsNone(rc)
            self.assertEqual(buf.getvalue(), "")

    def test_main_missing_db_exits_zero_without_calling_daemon(self):
        from saltmdb.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.db")
            out, err = io.StringIO(), io.StringIO()
            with (
                patch("sys.argv", ["saltmdb-cli", "--db-path", missing, "bootstrap-digest"]),
                patch("saltmdb.daemon.client.call") as call,
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                with self.assertRaises(SystemExit) as exit_info:
                    main()
            self.assertEqual(exit_info.exception.code, 0)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")
            call.assert_not_called()

    def test_prints_daemon_digest_verbatim(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            fake_digest = "<saltmdb-digest>\n\n<core-rules>\n\n</core-rules>\n\n</saltmdb-digest>"
            with patch("saltmdb.daemon.client.call", return_value=fake_digest) as mock_call:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_bootstrap_digest(_Args(db_path=tmp.name))
            self.assertIsNone(rc)
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
            self.assertIsNone(rc)
            self.assertIn("Warning", err.getvalue())

    def test_non_string_daemon_response_falls_back_to_empty_digest(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch("saltmdb.daemon.client.call", return_value=None):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_bootstrap_digest(_Args(db_path=tmp.name))
            self.assertIsNone(rc)
            self.assertIn("<saltmdb-digest>", buf.getvalue())


class TestSessionDigestCli(unittest.TestCase):
    """cmd_session_digest mirrors cmd_bootstrap_digest exactly: db-path existence
    short-circuit, daemon dispatch (passing the resolved cwd through), and never crashing
    the caller. Rendering itself lives in session_digest_service, tested separately."""

    def test_missing_db_returns_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "does-not-exist.db")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_session_digest(_Args(db_path=missing_path))
            self.assertIsNone(rc)
            self.assertEqual(buf.getvalue(), "")

    def test_main_missing_db_exits_zero_without_calling_daemon(self):
        from saltmdb.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.db")
            out, err = io.StringIO(), io.StringIO()
            with (
                patch("sys.argv", ["saltmdb-cli", "--db-path", missing, "session-digest"]),
                patch("saltmdb.daemon.client.call") as call,
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                with self.assertRaises(SystemExit) as exit_info:
                    main()
            self.assertEqual(exit_info.exception.code, 0)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")
            call.assert_not_called()

    def test_prints_daemon_digest_verbatim_and_passes_cwd(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            fake_digest = "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>"
            with patch("saltmdb.daemon.client.call", return_value=fake_digest) as mock_call:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_session_digest(_Args(db_path=tmp.name))
            self.assertIsNone(rc)
            self.assertIn(fake_digest, buf.getvalue())
            mock_call.assert_called_once_with(
                tmp.name,
                "get_last_session_digest",
                {"cwd": os.path.realpath(os.getcwd()), "max_chars": 40000},
            )

    def test_daemon_failure_never_crashes_caller(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch("saltmdb.daemon.client.call", side_effect=RuntimeError("boom")):
                buf = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(buf), redirect_stderr(err):
                    rc = cmd_session_digest(_Args(db_path=tmp.name))
            self.assertIsNone(rc)
            self.assertIn("Warning", err.getvalue())

    def test_non_string_daemon_response_falls_back_to_empty_digest(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch("saltmdb.daemon.client.call", return_value=None):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_session_digest(_Args(db_path=tmp.name))
            self.assertIsNone(rc)
            self.assertIn("<saltmdb-last-session-digest>", buf.getvalue())

    def test_max_chars_and_before_are_forwarded_normalized(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            args = _Args(db_path=tmp.name, max_chars=12000, before="2026-10-12T14:34:56+02:00")
            with (
                patch("saltmdb.config.get_handover_max_chars", return_value=40000),
                patch("saltmdb.daemon.client.call", return_value="<digest>") as mock_call,
            ):
                rc = cmd_session_digest(args)
        self.assertIsNone(rc)
        self.assertEqual(
            mock_call.call_args.args[2],
            {
                "cwd": os.path.realpath(os.getcwd()),
                "max_chars": 12000,
                "before": "2026-10-12T12:34:56.000000+00:00",
            },
        )

    def test_invalid_before_warns_and_is_ignored(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            args = _Args(db_path=tmp.name, max_chars=None, before="not-a-timestamp")
            err = io.StringIO()
            with (
                patch("saltmdb.config.get_handover_max_chars", return_value=40000),
                patch("saltmdb.daemon.client.call", return_value="<digest>") as mock_call,
                redirect_stderr(err),
            ):
                rc = cmd_session_digest(args)
        self.assertIsNone(rc)
        self.assertIn("Ignoring invalid --before", err.getvalue())
        self.assertNotIn("before", mock_call.call_args.args[2])

    def test_max_chars_is_limited_by_environment_and_zero_disables_handover(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            args = _Args(db_path=tmp.name, max_chars=8000, before=None)
            with (
                patch("saltmdb.config.get_handover_max_chars", return_value=5000),
                patch("saltmdb.daemon.client.call", return_value="<digest>") as limited,
            ):
                cmd_session_digest(args)
            with (
                patch("saltmdb.config.get_handover_max_chars", return_value=0),
                patch("saltmdb.daemon.client.call", return_value="<digest>") as zero,
            ):
                cmd_session_digest(args)
        self.assertEqual(limited.call_args.args[2]["max_chars"], 5000)
        self.assertEqual(zero.call_args.args[2]["max_chars"], 0)


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

    def test_session_digest_budget_and_before_options(self):
        args = build_parser().parse_args(
            ["session-digest", "--max-chars", "1234", "--before", "2026-10-12T00:00:00"]
        )
        self.assertEqual(args.max_chars, 1234)
        self.assertEqual(args.before, "2026-10-12T00:00:00")

    def test_export_corpus_snapshot_subcommand_has_no_owner_argument(self):
        parser = build_parser()
        args = parser.parse_args(["export-corpus-snapshot"])
        self.assertFalse(hasattr(args, "agent_id"))

    def test_export_corpus_snapshot_subcommand_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["export-corpus-snapshot"])
        self.assertIsNone(args.page_size)
        self.assertFalse(args.include_archived)
        self.assertIsNone(args.out)

    def test_orphans_subcommand_has_no_owner_argument(self):
        parser = build_parser()
        args = parser.parse_args(["orphans"])
        self.assertFalse(hasattr(args, "agent_id"))

    def test_corpus_health_subcommand_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["corpus-health"])
        self.assertEqual(args.days, 7)
        self.assertEqual(args.telemetry_limit, 10)

    def test_daemon_parser_surface(self):
        parser = build_parser()
        install = parser.parse_args(["daemon", "install-service", "--dry-run", "--no-start"])
        self.assertTrue(install.dry_run)
        self.assertTrue(install.no_start)
        self.assertTrue(callable(install.func))
        for command in ("start", "stop", "status", "uninstall-service"):
            args = parser.parse_args(["daemon", command])
            self.assertTrue(callable(args.func), command)
        self.assertEqual(
            parser.description,
            "SALTMDB CLI (read-only data commands plus daemon lifecycle).",
        )

    def test_daemon_status_command_prints_service_result(self):
        from saltmdb.daemon.service import ServiceResult

        args = build_parser().parse_args(["daemon", "status"])
        with patch(
            "saltmdb.daemon.service.daemon_status",
            return_value=ServiceResult(3, "not running"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = args.func(args)
        self.assertEqual(result, 3)
        self.assertEqual(buf.getvalue(), "not running\n")


class _OrphansArgs:
    def __init__(self, db_path=None):
        self.db_path = db_path


class TestOrphansCli(unittest.TestCase):
    """Phase 7 item 30: orphan detection moved off MCP -- cmd_orphans is a thin wrapper around
    the pre-existing memory_service.detect_orphaned_memories, unit-tested on its own already;
    this only covers the CLI's own responsibilities (configured-owner passthrough, JSON output, exit
    code on error)."""

    def test_passes_configured_owner_and_prints_json(self):
        fake_result = {"total_orphans": 1, "orphaned_memories": [{"id": "e1"}]}
        with (
            patch.dict(os.environ, {"SALTMDB_AGENT_ID": "alice"}),
            patch(
                "saltmdb.domain.services.memory_service.detect_orphaned_memories",
                return_value=fake_result,
            ) as mock_detect,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_orphans(_OrphansArgs(db_path="/tmp/whatever.db"))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), fake_result)
        mock_detect.assert_called_once_with(agent_id="alice", db_path="/tmp/whatever.db")

    def test_error_result_returns_nonzero_exit(self):
        with (
            patch.dict(os.environ, {"SALTMDB_AGENT_ID": "alice"}),
            patch(
                "saltmdb.domain.services.memory_service.detect_orphaned_memories",
                return_value={"error": "boom"},
            ),
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
        self.assertIsNone(rc)
        self.assertEqual(buf.getvalue(), "")

    def test_main_missing_db_exits_zero_without_calling_daemon(self):
        from saltmdb.cli import main

        missing = os.path.join(self.temp_dir, "missing.db")
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["saltmdb-cli", "--db-path", missing, "corpus-health"]),
            patch("saltmdb.daemon.client.call") as call,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as exit_info:
                main()
        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(out.getvalue(), "")
        call.assert_not_called()

    def test_empty_db_reports_all_zeros(self):
        rc, report = self._run()
        self.assertIsNone(rc)
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
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, agent_id, "
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
        self.assertIsNone(rc)
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
        self.assertIsNone(rc)
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
        self.assertIsNone(rc)
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
        self.assertIsNone(rc)
        self.assertEqual(report["telemetry"]["total_calls"], 3)
        self.assertEqual(len(report["telemetry"]["top_error_codes"]), 2)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cmd_corpus_health(_CorpusHealthArgs(db_path=self.db_path, days=0, telemetry_limit=10))
        report2 = json.loads(buf2.getvalue())
        self.assertEqual(
            report2["telemetry"]["total_calls"], 0, "days=0 must exclude rows just inserted 'now'"
        )

    def test_corrections_days_zero_literal_characterization(self):
        from datetime import timedelta

        future_entity = self._mk_entity("Future days-zero entity")
        future_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE entities SET created_at = ? WHERE id = ?", (future_at, future_entity)
        )
        self.conn.commit()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_corpus_health(_CorpusHealthArgs(db_path=self.db_path, days=0))
        self.assertIsNone(rc)
        self.assertEqual(
            json.loads(buf.getvalue())["corrections"],
            {
                "note": (
                    "revisions and supersessions create new entities, so those entities are included "
                    "in memories_created; the effective correction window is capped at 365 days; rows "
                    "outside the window's weeks are counted in unparsed_rows"
                ),
                "window_days": 0,
                "revises": 0,
                "supersedes": 0,
                "corrects": 0,
                "total": 0,
                "memories_created": 0,
                "by_week": [],
                "unparsed_rows": 0,
            },
        )

    def test_corrections_count_history_in_bounded_windows(self):
        from datetime import timedelta

        with self.subTest(corpus="empty"):
            rc, report = self._run()
            self.assertIsNone(rc)
            corrections = report["corrections"]
            self.assertEqual(corrections["total"], 0)
            self.assertEqual(corrections["memories_created"], 0)
            self.assertEqual(corrections["unparsed_rows"], 0)
            self.assertTrue(1 <= len(corrections["by_week"]) <= 2)
            self.assertTrue(
                all(
                    week["corrections"] == 0 and week["memories_created"] == 0
                    for week in corrections["by_week"]
                )
            )
            self.assertEqual(report["freshness"]["active"], 0)
            self.assertIsNone(report["freshness"]["share"])

        now = datetime.now(UTC)
        a = self._mk_entity("Revision", status="archived")
        b = self._mk_entity("Supersession")
        old = self._mk_entity("Outside bounded history")
        malformed = self._mk_entity("Malformed creation timestamp")
        for entity_id, created_at in (
            (a, (now - timedelta(days=1)).isoformat()),
            (b, (now - timedelta(days=2)).isoformat()),
            (old, (now - timedelta(days=400)).isoformat()),
            (malformed, "unparseable"),
        ):
            self.conn.execute(
                "UPDATE entities SET created_at = ? WHERE id = ?", (created_at, entity_id)
            )
        for source, target, predicate, created_at, valid_to in (
            (a, b, "revises", (now - timedelta(days=1)).isoformat(), None),
            (a, b, "supersedes", (now - timedelta(days=2)).isoformat(), now.isoformat()),
            (a, b, "corrects", (now - timedelta(days=20)).isoformat(), None),
            (old, a, "corrects", (now - timedelta(days=400)).isoformat(), None),
            (b, a, "corrects", "unparseable", None),
            (b, a, "related_to", (now - timedelta(days=1)).isoformat(), None),
        ):
            self.conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, created_at, "
                "valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    source,
                    target,
                    predicate,
                    created_at,
                    now.isoformat(),
                    valid_to,
                ),
            )
        self.conn.commit()

        for days, expected in (
            (7, (1, 1, 0, 2, 2, 2)),
            (30, (1, 1, 1, 3, 2, 2)),
            (0, (0, 0, 0, 0, 0, 0)),
            (-7, (0, 0, 0, 0, 0, 0)),
            (400, (1, 1, 1, 3, 2, 2)),
        ):
            with self.subTest(days=days):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cmd_corpus_health(_CorpusHealthArgs(db_path=self.db_path, days=days))
                self.assertIsNone(rc)
                corrections = json.loads(buf.getvalue())["corrections"]
                self.assertEqual(corrections["window_days"], days)
                self.assertEqual(
                    tuple(
                        corrections[key]
                        for key in (
                            "revises",
                            "supersedes",
                            "corrects",
                            "total",
                            "memories_created",
                            "unparsed_rows",
                        )
                    ),
                    expected,
                )
                weeks = corrections["by_week"]
                self.assertEqual(sum(week["corrections"] for week in weeks), expected[3])
                self.assertEqual(sum(week["memories_created"] for week in weeks), expected[4])
                if days <= 0:
                    self.assertEqual(weeks, [])
                else:
                    starts = [datetime.fromisoformat(week["week_start"]) for week in weeks]
                    self.assertTrue(all(start.weekday() == 0 for start in starts))
                    self.assertEqual(starts, sorted(starts))
                    self.assertTrue(
                        all(
                            right - left == timedelta(days=7)
                            for left, right in zip(starts, starts[1:])
                        )
                    )
                    self.assertLessEqual(len(weeks), 54)

    def test_future_rows_outside_week_buckets_are_unparsed(self):
        from datetime import timedelta

        now = datetime.now(UTC)
        source = self._mk_entity("Future correction source")
        target = self._mk_entity("Future correction target")
        old_at = (now - timedelta(days=8)).isoformat()
        for entity_id in (source, target):
            self.conn.execute(
                "UPDATE entities SET created_at = ? WHERE id = ?", (old_at, entity_id)
            )
        future_entity = self._mk_entity("Future entity")
        future_at = (now + timedelta(days=7)).isoformat()
        self.conn.execute(
            "UPDATE entities SET created_at = ? WHERE id = ?", (future_at, future_entity)
        )
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, "
            "valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                source,
                target,
                "corrects",
                future_at,
                now.isoformat(),
                None,
            ),
        )
        self.conn.commit()

        rc, report = self._run()
        self.assertIsNone(rc)
        corrections = report["corrections"]
        self.assertEqual(corrections["total"], 0)
        self.assertEqual(corrections["unparsed_rows"], 2)
        self.assertEqual(corrections["memories_created"], 0)
        self.assertEqual(sum(week["corrections"] for week in corrections["by_week"]), 0)
        self.assertEqual(sum(week["memories_created"] for week in corrections["by_week"]), 0)

    def test_freshness_uses_fixed_30_day_window_and_excludes_archived(self):
        from datetime import timedelta

        now = datetime.now(UTC)
        for title, age in (
            ("Updated Yesterday", 1),
            ("Updated Last Month", 40),
            ("Updated Long Ago", 50),
            ("Archived Yesterday", 1),
        ):
            status = "archived" if title.startswith("Archived") else "raw"
            entity_id = self._mk_entity(title, status=status)
            self.conn.execute(
                "UPDATE entities SET updated_at = ? WHERE id = ?",
                ((now - timedelta(days=age)).isoformat(), entity_id),
            )
        self.conn.commit()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_corpus_health(_CorpusHealthArgs(db_path=self.db_path, days=0))
        self.assertIsNone(rc)
        freshness = json.loads(buf.getvalue())["freshness"]
        self.assertEqual(freshness["window_days"], 30)
        self.assertEqual(freshness["active"], 3)
        self.assertEqual(freshness["updated_in_window"], 1)
        self.assertEqual(freshness["share"], 0.333)


class TestSnapshotCli(unittest.TestCase):
    """SPEC-DB-SNAPSHOT-CLI T8: `saltmdb-cli snapshot` through main(); the daemon call is patched."""

    def _main(self, argv):
        from saltmdb.cli import main

        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["saltmdb-cli", *argv]), redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as exit_info:
                main()
        return exit_info.exception.code, out.getvalue(), err.getvalue()

    def test_success_prints_path_and_exits_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch(
                "saltmdb.daemon.client.call_method", return_value={"path": "/snap/s.db"}
            ) as call:
                code, out, _err = self._main(["--db-path", tmp.name, "snapshot"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "/snap/s.db")
        call.assert_called_once_with(tmp.name, "create_snapshot_now", {})

    def test_missing_db_exits_one_without_calling_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.db")
            with patch("saltmdb.daemon.client.call_method") as call:
                code, out, err = self._main(["--db-path", missing, "snapshot"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("# Error: no database at", err)
        call.assert_not_called()

    def test_daemon_failure_exits_one_with_error_on_stderr(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch("saltmdb.daemon.client.call_method", side_effect=RuntimeError("boom")):
                code, out, err = self._main(["--db-path", tmp.name, "snapshot"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("# Error:", err)
        self.assertIn("boom", err)

    def test_relative_dest_dir_reaches_daemon_as_absolute(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with patch(
                "saltmdb.daemon.client.call_method", return_value={"path": "/snap/s.db"}
            ) as call:
                code, _out, _err = self._main(
                    ["--db-path", tmp.name, "snapshot", "--dest-dir", "rel/out"]
                )
        self.assertEqual(code, 0)
        sent = call.call_args.args[2]["dest_dir"]
        self.assertTrue(os.path.isabs(sent))
        self.assertEqual(sent, os.path.abspath("rel/out"))


if __name__ == "__main__":
    unittest.main()
