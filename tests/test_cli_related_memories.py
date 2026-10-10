"""CLI contracts for related-memories; all daemon/model behavior is patched."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest.mock import patch

from saltmdb import cli


class TestRelatedMemoriesCli(unittest.TestCase):
    def _run_main(
        self,
        argv: list[str],
        *,
        stdin_text: str = "",
        result=None,
        side_effect=None,
        patch_call: bool = True,
    ):
        out = io.StringIO()
        err = io.StringIO()
        call_patch = (
            patch("saltmdb.daemon.client.call", return_value=result, side_effect=side_effect)
            if patch_call
            else nullcontext()
        )
        with (
            patch.object(sys, "argv", ["saltmdb-cli", *argv]),
            patch.object(sys, "stdin", io.StringIO(stdin_text)),
            call_patch,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        return raised.exception.code, out.getvalue(), err.getvalue()

    def test_text_mode_flattens_title_and_cuts_at_one_hundred_fifty_chars(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            title = "x" * 149 + "\tnewline\ntrailing"
            code, stdout, stderr = self._run_main(
                ["--db-path", db.name, "related-memories"],
                stdin_text="A sufficiently long answer-side text for lookup.",
                result=[{"id": "memory-1", "title": title, "score": 7.0}],
            )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "memory-1\t" + "x" * 149 + " \n")
        self.assertEqual(stderr, "")

    def test_json_mode_emits_every_scored_candidate_with_contract_keys(self):
        result = [
            {
                "id": "memory-1",
                "title": "A title",
                "score": 6.5,
                "segment": 1,
                "fts_rank": None,
                "semantic_distance": 0.2,
            }
        ]
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            code, stdout, stderr = self._run_main(
                ["--db-path", db.name, "related-memories", "--json", "--min-score", "9"],
                stdin_text="query text",
                result=result,
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            set(json.loads(stdout)[0]),
            {"id", "title", "score", "segment", "fts_rank", "semantic_distance"},
        )

    def test_missing_daemon_is_best_effort_and_never_spawns(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".db") as db,
            patch("saltmdb.daemon.client.reachable_daemon_info", return_value=None) as reachable,
            patch("saltmdb.daemon.client.ensure_daemon_running") as ensure,
        ):
            code, stdout, stderr = self._run_main(
                ["--db-path", db.name, "related-memories"],
                stdin_text="query text",
                patch_call=False,
            )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("daemon", stderr.lower())
        reachable.assert_called()
        ensure.assert_not_called()

    def test_missing_db_is_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "missing.db")
            code, stdout, stderr = self._run_main(
                ["--db-path", missing, "related-memories"], stdin_text="query text"
            )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_timeout_path_exits_zero_without_waiting_for_worker(self):
        script = """
import time
from saltmdb import cli
from unittest.mock import patch

def slow(*args, **kwargs):
    time.sleep(2)

with patch('saltmdb.daemon.client.call', side_effect=slow):
    cli.main()
"""
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            start = time.monotonic()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    "--db-path",
                    db.name,
                    "related-memories",
                    "--timeout-ms",
                    "200",
                ],
                input="query text",
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONPATH": os.path.abspath("src")},
                check=False,
            )
        elapsed = time.monotonic() - start
        self.assertEqual(completed.returncode, 0)
        self.assertLess(elapsed, 1.2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("timed out", completed.stderr.lower())

    def test_parser_exposes_related_memory_defaults(self):
        from saltmdb import config

        args = cli.build_parser().parse_args(["related-memories"])
        self.assertEqual(args.limit, config.RELATED_MEMORIES_DEFAULT_LIMIT)
        self.assertEqual(args.min_score, config.RELATED_MEMORIES_MIN_SCORE)
        self.assertEqual(args.timeout_ms, 4000)
        self.assertFalse(args.json)
        self.assertEqual(args.exclude_ids, "")

    def test_agent_id_is_raw_and_whitespace_only_becomes_none(self):
        for env_value, expected_agent_id in (("  \t", None), ("  agent-a  ", "agent-a")):
            with self.subTest(env_value=env_value), tempfile.NamedTemporaryFile(suffix=".db") as db:
                out = io.StringIO()
                err = io.StringIO()
                with (
                    patch.object(
                        sys, "argv", ["saltmdb-cli", "--db-path", db.name, "related-memories"]
                    ),
                    patch.object(sys, "stdin", io.StringIO("query text")),
                    patch.dict(os.environ, {"SALTMDB_AGENT_ID": env_value}),
                    patch("saltmdb.daemon.client.call", return_value=[]) as call_mock,
                    redirect_stdout(out),
                    redirect_stderr(err),
                ):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()
                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(call_mock.call_args.args[2]["agent_id"], expected_agent_id)

    def test_exclude_ids_drop_empty_items_and_whitespace(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            out = io.StringIO()
            err = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "saltmdb-cli",
                        "--db-path",
                        db.name,
                        "related-memories",
                        "--exclude-ids",
                        " first, ,second ,, third ",
                    ],
                ),
                patch.object(sys, "stdin", io.StringIO("query text")),
                patch("saltmdb.daemon.client.call", return_value=[]) as call_mock,
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(call_mock.call_args.args[2]["exclude_ids"], ["first", "second", "third"])

    def test_usage_error_still_uses_argparse_exit_two(self):
        with self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(["related-memories", "--min-score", "not-a-number"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
