import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from saltmdb.cli import build_parser, cmd_bootstrap_digest


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


if __name__ == "__main__":
    unittest.main()
