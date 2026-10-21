"""Behavioral contracts for bounded pre-compaction sweep input."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

common = importlib.import_module("_saltmdb_hook_common")

TRANSCRIPT_PATH = "/fictional/session.jsonl"
LINE_END = "\n"
CONTENT = '{"type":"user","text":"decision"}' + LINE_END
BOUNDARY = '{"type":"system","subtype":"compact_boundary"}' + LINE_END
DEFAULT_BYTES = 262144


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.delenv("SALTMDB_HOOK_PREFERRED_AGENT", raising=False)
    monkeypatch.delenv("SALTMDB_PRECOMPACT_MAX_BYTES", raising=False)
    spec = importlib.util.spec_from_file_location(
        "pre_compact_under_test", HOOKS_DIR / "saltmdb-pre-compact-sweep.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure_main(
    sweep: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    text: str | None,
    result: bool = True,
) -> Mock:
    monkeypatch.setattr(
        sweep,
        "read_stdin_json",
        Mock(return_value={"transcript_path": TRANSCRIPT_PATH}),
    )
    monkeypatch.setattr(sweep, "read_transcript", Mock(return_value=text))
    monkeypatch.setattr(sweep.shutil, "which", Mock(return_value="/fake/agent"))
    runner = Mock(return_value=result)
    monkeypatch.setattr(sweep, "run_quiet", runner)
    return runner


def test_constant_argv_and_transcript_stdin(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure_main(sweep, monkeypatch, CONTENT)
    sweep.main()
    runner.assert_called_once_with(
        ["codex", "exec", sweep.SWEEP_PROMPT], sweep.TIMEOUT_SECS, CONTENT
    )
    assert TRANSCRIPT_PATH not in str(runner.call_args)


def test_preferred_claude_runs_first(sweep: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALTMDB_HOOK_PREFERRED_AGENT", "claude")
    runner = _configure_main(sweep, monkeypatch, CONTENT)
    sweep.main()
    assert runner.call_args_list[0].args[0][:2] == ["claude", "-p"]


def test_failed_first_cli_falls_back(sweep: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _configure_main(sweep, monkeypatch, CONTENT, result=False)
    runner.side_effect = [False, True]
    sweep.main()
    assert [call.args[0][:2] for call in runner.call_args_list] == [
        ["codex", "exec"],
        ["claude", "-p"],
    ]


@pytest.mark.parametrize("text", [None, "", BOUNDARY])
def test_empty_payload_does_not_run_child(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    runner = _configure_main(sweep, monkeypatch, text)
    with pytest.raises(SystemExit) as raised:
        sweep.main()
    assert raised.value.code == 0
    runner.assert_not_called()


def test_missing_transcript_path_does_not_run_child(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "read_stdin_json", Mock(return_value={}))
    runner = Mock()
    monkeypatch.setattr(sweep, "run_quiet", runner)
    with pytest.raises(SystemExit) as raised:
        sweep.main()
    assert raised.value.code == 0
    runner.assert_not_called()


def test_denied_transcript_does_not_run_child(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure_main(sweep, monkeypatch, None)
    with pytest.raises(SystemExit) as raised:
        sweep.main()
    assert raised.value.code == 0
    runner.assert_not_called()


def test_marker_requires_expected_record_type(sweep: ModuleType) -> None:
    nested = '{"type":"user","nested":{"subtype":"compact_boundary"}}' + LINE_END
    assert sweep._sweep_input(nested + "keep" + LINE_END) == nested + "keep" + LINE_END


def test_marker_subtype_with_wrong_type_is_ignored(sweep: ModuleType) -> None:
    wrong_type = '{"type":"user","subtype":"compact_boundary"}' + LINE_END
    assert sweep._sweep_input(wrong_type + "keep" + LINE_END) == wrong_type + "keep" + LINE_END


def test_marker_type_with_wrong_subtype_is_ignored(sweep: ModuleType) -> None:
    wrong_subtype = (
        '{"type":"system","note":{"subtype":"compact_boundary"},"subtype":"other"}' + LINE_END
    )
    assert (
        sweep._sweep_input(wrong_subtype + "keep" + LINE_END) == wrong_subtype + "keep" + LINE_END
    )


def test_malformed_marker_record_is_ignored(sweep: ModuleType) -> None:
    malformed = '{"subtype":"compact_boundary"' + LINE_END
    assert sweep._sweep_input(malformed + "keep" + LINE_END) == malformed + "keep" + LINE_END


def test_last_valid_marker_wins(sweep: ModuleType) -> None:
    assert sweep._sweep_input(BOUNDARY + "old" + LINE_END + BOUNDARY + "new" + LINE_END) == (
        "new" + LINE_END
    )


def test_cap_keeps_tail_at_line_boundary(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", "1024")
    text = "a" * 200 + LINE_END + "b" * 1000 + LINE_END
    result = sweep._sweep_input(text)
    assert len(result.encode()) <= 1024
    assert result.endswith("b" * 1000 + LINE_END)
    assert not result.startswith("a")


def test_cut_at_line_start_keeps_complete_line(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", "1024")
    line = "x" * 1024
    assert sweep._sweep_input("prefix" + LINE_END + line) == line


def test_multibyte_cut_drops_partial_character(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", "1024")
    result = sweep._sweep_input("é" * 600)
    assert len(result.encode("utf-8")) <= 1024
    result.encode("utf-8").decode("utf-8")


def test_three_byte_character_straddling_cut_is_valid_utf8(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", "1024")
    result = sweep._sweep_input("a" * 1023 + "あ" * 599)
    payload = result.encode("utf-8")
    assert len(payload) <= 1024
    payload.decode("utf-8")


def test_long_last_line_with_trailing_newline_stays_nonempty(sweep: ModuleType) -> None:
    result = sweep._sweep_input("x" + LINE_END + "a" * 300000 + LINE_END)
    payload = result.encode("utf-8")
    assert payload
    assert len(payload) == DEFAULT_BYTES


@pytest.mark.parametrize("value", ["0", "1023", "4194305", "abc", "-1"])
def test_invalid_cap_uses_default(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", value)
    assert len(sweep._sweep_input("z" * (DEFAULT_BYTES + 10)).encode()) <= DEFAULT_BYTES


@pytest.mark.parametrize("value", ["1024", "4194304"])
def test_cap_bounds_are_inclusive(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    limit = int(value)
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", value)
    assert len(sweep._sweep_input("z" * (limit + 10)).encode()) <= limit


def test_maximum_cap_override_exceeds_default(
    sweep: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALTMDB_PRECOMPACT_MAX_BYTES", "4194304")
    result = sweep._sweep_input("z" * (DEFAULT_BYTES + 10))
    assert len(result.encode()) > DEFAULT_BYTES


def test_run_quiet_empty_string_passes_empty_bytes() -> None:
    completed = Mock(returncode=0)
    with patch.object(common.subprocess, "run", return_value=completed) as run:
        assert common.run_quiet(["fake"], 2, "") is True
        assert run.call_args.kwargs["input"] == b""
        assert "stdin" not in run.call_args.kwargs


def test_run_quiet_passes_optional_bytes_input() -> None:
    completed = Mock(returncode=0)
    with patch.object(common.subprocess, "run", return_value=completed) as run:
        assert common.run_quiet(["fake"], 2, "x") is True
        assert run.call_args.kwargs["input"] == b"x"
    with patch.object(common.subprocess, "run", return_value=completed) as run:
        assert common.run_quiet(["fake"], 2) is True
        assert run.call_args.kwargs["input"] is None
        assert "stdin" not in run.call_args.kwargs


def test_run_quiet_handles_process_failures() -> None:
    with patch.object(common.subprocess, "run", side_effect=OSError):
        assert common.run_quiet(["fake"], 2, "x") is False
    with patch.object(
        common.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired("fake", 2),
    ):
        assert common.run_quiet(["fake"], 2, "x") is False
