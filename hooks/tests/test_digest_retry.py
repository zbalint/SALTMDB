"""Behavioral tests for the SessionStart marker and UserPromptSubmit retry hook."""

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))
import _saltmdb_hook_common as common  # noqa: E402


def _load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bootstrap():
    return _load_module("saltmdb-session-start-bootstrap.py", "bootstrap_under_test")


def _retry():
    return _load_module("saltmdb-prompt-submit-digest-retry.py", "retry_under_test")


def _stdin(payload: dict):
    return patch("sys.stdin", io.StringIO(json.dumps(payload)))


def _marker(tmp: Path, session_id: str = "s") -> Path:
    with patch.object(Path, "home", return_value=tmp):
        return common.digest_pending_path(session_id)


def test_session_marker_exists_before_first_cli_call(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    marker = _marker(tmp_path)
    observed: list[dict] = []

    def fake_run_cli(*args):
        observed.append(json.loads(marker.read_text()))
        return {"bootstrap-digest": "<boot>", "session-digest": "<session>"}.get(args[0])

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=fake_run_cli):
            bootstrap.main()
    assert observed[0]["missing"] == ["bootstrap-digest", "session-digest"]
    assert observed[0]["running_since"] is not None


def test_session_all_parts_returned_removes_marker_and_prints(capsys, tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    values = {"bootstrap-digest": "<boot>", "session-digest": "<session>", "corpus-health": ""}
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=lambda *args: values[args[0]]):
            bootstrap.main()
    assert "<boot>" in capsys.readouterr().out
    assert not _marker(tmp_path).exists()


def test_session_partial_fetch_keeps_empty_bootstrap(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    values = {"bootstrap-digest": "", "session-digest": "<session>", "corpus-health": ""}
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=lambda *args: values[args[0]]):
            bootstrap.main()
    pending = json.loads(_marker(tmp_path).read_text())
    assert isinstance(pending["started_at"], (int, float))
    assert {key: value for key, value in pending.items() if key != "started_at"} == {
        "missing": ["bootstrap-digest"],
        "attempts": 0,
        "running_since": None,
    }


def test_session_empty_session_part_stays_pending(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    values = {"bootstrap-digest": "<boot>", "session-digest": "", "corpus-health": ""}
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=lambda *args: values[args[0]]):
            bootstrap.main()
    assert json.loads(_marker(tmp_path).read_text())["missing"] == ["session-digest"]


def test_compact_session_preserves_previous_session_part(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["bootstrap-digest", "session-digest"])
    calls: list[str] = []

    def fake_run_cli(*args):
        calls.append(args[0])
        return {"bootstrap-digest": "<boot>", "corpus-health": ""}[args[0]]

    with (
        patch.object(Path, "home", return_value=tmp_path),
        _stdin({"source": "compact", "session_id": "s"}),
    ):
        with patch.object(bootstrap, "run_cli", side_effect=fake_run_cli):
            bootstrap.main()
    assert calls == ["bootstrap-digest", "corpus-health"]
    assert json.loads(marker.read_text())["missing"] == ["session-digest"]


def test_session_without_cli_or_session_id_writes_no_marker(tmp_path):
    bootstrap = _bootstrap()
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": ""}):
        bootstrap.CLI = "/fake-cli"
        with patch.object(bootstrap, "run_cli", return_value=None):
            bootstrap.main()
    assert not list((tmp_path / ".saltmdb" / "hooks" / ".state").glob("digest-" + "pending-*"))

    bootstrap.CLI = None
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        bootstrap.main()
    assert not _marker(tmp_path).exists()


def test_session_deadline_leaves_pending_marker_and_returns_quickly(tmp_path, capsys):
    bootstrap = _bootstrap()
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nexec sleep 30\n")
    slow.chmod(0o755)
    bootstrap.CLI = str(slow)
    bootstrap.BUDGET_S = 2.0
    started = time.monotonic()
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        bootstrap.main()
    assert time.monotonic() - started < 6
    assert "<saltmdb-digest-pending>" in capsys.readouterr().out
    pending = json.loads(_marker(tmp_path).read_text())
    assert pending["missing"] == ["bootstrap-digest", "session-digest"]


def test_retry_without_marker_skips_cli_and_output(tmp_path, capsys):
    retry = _retry()
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli") as resolve,
            patch.object(retry, "run_cli_bounded") as runner,
        ):
            retry.main()
    resolve.assert_not_called()
    runner.assert_not_called()
    assert capsys.readouterr().out == ""


def test_retry_success_prints_parts_and_removes_claim(tmp_path, capsys):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["bootstrap-digest", "session-digest"])
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(
                retry, "run_cli_bounded", side_effect=lambda _cli, args, _remaining: f"<{args[0]}>"
            ),
        ):
            retry.main()
    assert capsys.readouterr().out == "<bootstrap-digest>\n\n<session-digest>\n"
    assert not marker.exists()
    assert not Path(f"{marker}.claimed").exists()


def test_retry_failure_increments_attempts_and_exhausts_after_three(tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["bootstrap-digest", "session-digest"])
    with patch.object(Path, "home", return_value=tmp_path):
        for expected_attempts in (1, 2):
            with (
                _stdin({"session_id": "s"}),
                patch.object(retry, "resolve_cli", return_value="/fake-cli"),
                patch.object(retry, "run_cli_bounded", return_value=None),
            ):
                retry.main()
            assert json.loads(marker.read_text())["attempts"] == expected_attempts
        with (
            _stdin({"session_id": "s"}),
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(retry, "run_cli_bounded", return_value=None),
        ):
            retry.main()
    assert not marker.exists()


def test_retry_second_run_after_success_is_quiet(tmp_path, capsys):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["bootstrap-digest"])
    with patch.object(Path, "home", return_value=tmp_path):
        with (
            _stdin({"session_id": "s"}),
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(retry, "run_cli_bounded", return_value="<boot>"),
        ):
            retry.main()
        with _stdin({"session_id": "s"}), patch.object(retry, "resolve_cli") as resolve:
            retry.main()
    assert capsys.readouterr().out == "<boot>\n"
    resolve.assert_not_called()


def test_session_prunes_old_pending_files(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    old = _marker(tmp_path, "old")
    common.write_digest_pending(old, ["bootstrap-digest"])
    old_time = time.time() - 2 * 86400
    os.utime(old, (old_time, old_time))
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "new"}):
        with patch.object(bootstrap, "run_cli", return_value=""):
            bootstrap.main()
    assert not old.exists()


def test_bounded_runner_honors_minimum_and_ten_second_cap():
    with patch.object(common.subprocess, "run") as run:
        assert common.run_cli_bounded("cli", ("x",), 0.9) is None
        run.assert_not_called()
        run.return_value = type("Result", (), {"returncode": 0, "stdout": "ok"})()
        assert common.run_cli_bounded("cli", ("x",), 20.0) == "ok"
    assert run.call_args.kwargs["timeout"] == 10


def test_output_cap_and_section_order(capsys, tmp_path):
    bootstrap = _bootstrap()
    long_cli = "/" + "x" * 299
    bootstrap.CLI = long_cli
    session = "x" * 25_000
    health = json.dumps({"overdue_core_reviews": {"count": 1, "entries": [{"title": "Core"}]}})

    def fake_run_cli(*args):
        return {"bootstrap-digest": "<core>", "session-digest": session, "corpus-health": health}[
            args[0]
        ]

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=fake_run_cli):
            bootstrap.main()
    output = capsys.readouterr().out
    assert len(output) <= 9800
    assert output.startswith("<core>")
    assert "<saltmdb-overdue-core-notice>" in output
    assert output.index("<saltmdb-overdue-core-notice>") < output.index("Core")
    assert "bootstrap-digest" in output and "session-digest" in output
    assert output.endswith(f"{long_cli} bootstrap-digest and {long_cli} session-digest.")
    assert common.cap_hook_output("x" * 9000, "/fake-cli") == "x" * 9000
    capped_long_path = common.cap_hook_output("x" * 25_000, long_cli)
    assert len(capped_long_path) <= 9800
    assert capped_long_path.endswith(f"{long_cli} bootstrap-digest and {long_cli} session-digest.")


def test_retry_output_cap(capsys, tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    long_cli = "/" + "x" * 299
    common.write_digest_pending(marker, ["bootstrap-digest", "session-digest"])
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value=long_cli),
            patch.object(retry, "run_cli_bounded", return_value="y" * 6000),
        ):
            retry.main()
    output = capsys.readouterr().out
    assert len(output) <= 9800
    assert output.startswith("y")
    assert output.endswith(f"{long_cli} bootstrap-digest and {long_cli} session-digest.")


def test_retry_running_grace_skips_young_marker_and_processes_old(tmp_path, capsys):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["bootstrap-digest"], running_since=time.time())
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(retry, "resolve_cli") as resolve:
            retry.main()
    resolve.assert_not_called()
    assert marker.exists()

    common.write_digest_pending(marker, ["bootstrap-digest"], running_since=time.time() - 14.9)
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(retry, "run_cli_bounded", return_value="<boot>"),
        ):
            retry.main()
    assert capsys.readouterr().out == "<boot>\n"
    assert not marker.exists()


def test_retry_claim_stale_recovery_and_mtime_refresh(tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    claimed = Path(f"{marker}.claimed")
    common.write_digest_pending(claimed, ["bootstrap-digest"])
    old = time.time() - 31
    os.utime(claimed, (old, old))
    observed_mtime: list[float] = []

    def runner(_cli, _args, _remaining):
        observed_mtime.append(claimed.stat().st_mtime)

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(retry, "run_cli_bounded", side_effect=runner),
        ):
            retry.main()
    assert observed_mtime and observed_mtime[0] > old
    assert json.loads(marker.read_text())["attempts"] == 1

    marker.unlink()
    common.write_digest_pending(claimed, ["bootstrap-digest"])
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(retry, "resolve_cli") as resolve:
            retry.main()
    resolve.assert_not_called()


def test_retry_claim_stale_stat_failure_skips_cli(tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    claimed_file = Path(f"{marker}.claimed")
    common.write_digest_pending(claimed_file, ["bootstrap-digest"])
    base_path = type(Path())

    class FailingPath(base_path):
        def is_file(self):
            return True

        def stat(self, *args, **kwargs):
            raise OSError

    claimed = FailingPath(claimed_file)
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(retry, "resolve_cli") as resolve:
            assert retry._claim_ready(marker, claimed) is None
    resolve.assert_not_called()
    assert claimed_file.exists()


def test_retry_claim_stale_replacement_failure_skips_cli(tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    claimed = Path(f"{marker}.claimed")
    common.write_digest_pending(claimed, ["bootstrap-digest"])
    old = time.time() - 31
    os.utime(claimed, (old, old))
    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry.os, "replace", side_effect=OSError),
            patch.object(retry, "resolve_cli") as resolve,
        ):
            retry.main()
    resolve.assert_not_called()
    assert claimed.exists()


def test_session_reconciliation_keeps_retry_write(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    marker = _marker(tmp_path)

    def fake_run_cli(*args):
        part = args[0]
        if part == "bootstrap-digest":
            common.write_digest_pending(marker, ["bootstrap-digest"], running_since=None)
            return ""
        if part == "session-digest":
            return "<session>"
        return ""

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=fake_run_cli):
            bootstrap.main()
    assert json.loads(marker.read_text())["missing"] == ["bootstrap-digest"]


def test_hooks_forward_budget_and_started_at_cutoff(tmp_path):
    bootstrap = _bootstrap()
    bootstrap.CLI = "/fake-cli"
    setattr(bootstrap, "STARTED_AT", 1_700_000_060.0)
    calls: list[tuple[str, ...]] = []

    def fake_run_cli(*args):
        calls.append(tuple(args))
        return {"bootstrap-digest": "<core>", "session-digest": "<session>", "corpus-health": ""}[
            args[0]
        ]

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with patch.object(bootstrap, "run_cli", side_effect=fake_run_cli):
            bootstrap.main()
    session_call = next(args for args in calls if args[0] == "session-digest")
    assert "--max-chars" in session_call
    assert "--before" in session_call
    assert session_call[session_call.index("--before") + 1].endswith("+00:00")


def test_retry_marker_without_started_at_omits_before(tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["session-digest"])
    calls: list[tuple[str, ...]] = []

    def runner(_cli, args, _remaining):
        calls.append(tuple(args))
        return "<session>"

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(retry, "run_cli_bounded", side_effect=runner),
        ):
            retry.main()
    assert calls == [("session-digest", "--max-chars", "9346")]


def test_retry_passes_marker_started_at_to_before_cutoff(tmp_path):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(
        marker, ["bootstrap-digest", "session-digest"], started_at=1_700_000_060.0
    )
    calls: list[tuple[str, ...]] = []

    def runner(_cli, args, _remaining):
        calls.append(tuple(args))
        return "<part>"

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value="/fake-cli"),
            patch.object(retry, "run_cli_bounded", side_effect=runner),
        ):
            retry.main()
    assert calls[0] == ("bootstrap-digest",)
    assert calls[1][0] == "session-digest"
    assert "--before" in calls[1]


def test_retry_caps_large_session_output_and_forwards_cutoff(tmp_path, capsys):
    retry = _retry()
    marker = _marker(tmp_path)
    common.write_digest_pending(marker, ["session-digest"], started_at=1_700_000_060.0)
    calls: list[tuple[str, ...]] = []

    def runner(_cli, args, _remaining):
        calls.append(tuple(args))
        return "x" * 25_000

    with patch.object(Path, "home", return_value=tmp_path), _stdin({"session_id": "s"}):
        with (
            patch.object(retry, "resolve_cli", return_value="/" + "x" * 299),
            patch.object(retry, "run_cli_bounded", side_effect=runner),
        ):
            retry.main()
    output = capsys.readouterr().out
    assert len(output) <= 9_800
    assert output.endswith(
        "/" + "x" * 299 + " bootstrap-digest and /" + "x" * 299 + " session-digest."
    )
    assert calls[0][0] == "session-digest"
    assert "--max-chars" in calls[0]
    assert "--before" in calls[0]
