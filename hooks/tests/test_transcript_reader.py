"""Contract tests for harness-owned transcript path validation and bounded reads."""

from __future__ import annotations

import importlib.util
import os
import stat
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

COMMON = Path(__file__).resolve().parents[1] / "_saltmdb_hook_common.py"
DENIAL = "saltmdb hook: transcript path denied or unreadable\n"
CONTENT = "first\nmatch.*\nlast match.*\n"
CLAUDE_ROOT = Path(".claude/projects")
CODEX_ROOT = Path(".codex/sessions")
COPILOT_ROOT = Path(".copilot/session-state")
OMP_ROOT = Path(".omp/agent/sessions")


@pytest.fixture
def reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    spec = importlib.util.spec_from_file_location("transcript_reader_under_test", COMMON)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def allowed(tmp_path: Path) -> Path:
    path = tmp_path / CLAUDE_ROOT / "project/session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(CONTENT, encoding="utf-8")
    return path


def assert_denied(reader: ModuleType, path: object, capsys: pytest.CaptureFixture[str]) -> None:
    setattr(reader, "_transcript_denial_emitted", False)
    assert reader.read_transcript(path) is None
    assert capsys.readouterr().err == DENIAL


def test_empty_and_none_are_silent(reader: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert reader.read_transcript("") is None
    assert reader.read_transcript(None) is None
    assert capsys.readouterr().err == ""


def test_existing_outside_file_is_denied(reader: ModuleType, tmp_path: Path, capsys) -> None:
    path = tmp_path / "outside.jsonl"
    path.write_text("private outside content")
    setattr(reader, "_transcript_denial_emitted", False)
    assert reader.read_transcript(str(path)) is None
    stderr = capsys.readouterr().err
    assert stderr == DENIAL
    assert str(path) not in stderr


def test_relative_escape_and_control_paths_are_denied(
    reader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = tmp_path / CLAUDE_ROOT
    root.mkdir(parents=True)
    (tmp_path / "secret.jsonl").write_text("secret")
    local = root / "session.jsonl"
    local.write_text("local")
    monkeypatch.chdir(root)
    assert_denied(reader, "session.jsonl", capsys)
    newline = root / "bad\nname.jsonl"
    newline.write_text("bad")
    assert_denied(reader, str(newline), capsys)
    assert_denied(reader, str(root / ".." / ".." / "secret.jsonl"), capsys)
    assert_denied(reader, 42, capsys)


def test_prefix_confusion_is_denied(reader: ModuleType, tmp_path: Path, capsys) -> None:
    for sibling in (
        tmp_path / ".claude/projects-evil/x.jsonl",
        tmp_path / ".claude/projectsX/x.jsonl",
    ):
        sibling.parent.mkdir(parents=True)
        sibling.write_text("private")
        assert_denied(reader, str(sibling), capsys)


def test_symlink_outside_is_denied(reader: ModuleType, tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("private")
    link = tmp_path / CLAUDE_ROOT / "link.jsonl"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert_denied(reader, str(link), capsys)


def test_symlinked_root_is_allowed(reader: ModuleType, tmp_path: Path, capsys) -> None:
    real_root = tmp_path / "real-projects" / "projects"
    real_root.mkdir(parents=True)
    path = real_root / "session.jsonl"
    path.write_text(CONTENT)
    link_root = tmp_path / ".claude"
    link_root.symlink_to(tmp_path / "real-projects", target_is_directory=True)
    assert reader.read_transcript(str(link_root / "projects" / "session.jsonl")) == CONTENT
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("relative_root", "env_name", "root"),
    [
        (False, "CLAUDE_CONFIG_DIR", CLAUDE_ROOT),
        (True, "CLAUDE_CONFIG_DIR", CLAUDE_ROOT),
        (False, "CODEX_HOME", CODEX_ROOT),
        (True, "CODEX_HOME", CODEX_ROOT),
    ],
)
def test_environment_roots_move_when_absolute(
    reader: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    relative_root: bool,
    env_name: str,
    root: Path,
) -> None:
    target_root = tmp_path / "configured"
    value = "relative/dir" if relative_root else str(target_root)
    monkeypatch.setenv(env_name, value)
    subdir = "projects" if env_name == "CLAUDE_CONFIG_DIR" else "sessions"
    configured = target_root / subdir / "x.jsonl"
    configured.parent.mkdir(parents=True)
    configured.write_text(CONTENT)
    if relative_root:
        fallback = tmp_path / root / "fallback.jsonl"
        fallback.parent.mkdir(parents=True)
        fallback.write_text(CONTENT)
        cwd_candidate = tmp_path / "projects" / "x.jsonl"
        cwd_candidate.parent.mkdir(parents=True)
        cwd_candidate.write_text(CONTENT)
        monkeypatch.chdir(tmp_path)
        assert reader.read_transcript(str(fallback)) == CONTENT
        assert_denied(reader, str(cwd_candidate), capsys)
        assert_denied(reader, str(configured), capsys)
    else:
        assert reader.read_transcript(str(configured)) == CONTENT
        assert capsys.readouterr().err == ""


def test_other_harness_roots_are_allowed(reader: ModuleType, tmp_path: Path, capsys) -> None:
    for root in (COPILOT_ROOT, OMP_ROOT):
        path = tmp_path / root / "session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(CONTENT)
        assert reader.read_transcript(str(path)) == CONTENT
    assert capsys.readouterr().err == ""


def test_unlisted_environment_root_is_denied(
    reader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = tmp_path / "unlisted"
    path = root / "projects/x.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(CONTENT)
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    monkeypatch.setenv("COPILOT_HOME", str(root))
    assert_denied(reader, str(path), capsys)


def test_directory_and_fifo_are_denied_without_hanging(
    reader: ModuleType, tmp_path: Path, capsys
) -> None:
    directory = tmp_path / CLAUDE_ROOT / "directory"
    directory.mkdir(parents=True)
    assert_denied(reader, str(directory), capsys)
    fifo = tmp_path / CLAUDE_ROOT / "fifo"
    os.mkfifo(fifo)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(reader.read_transcript(str(fifo))))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "FIFO open did not respect O_NONBLOCK"
    assert result == [None]
    assert capsys.readouterr().err == ""


def test_size_cap_is_read_at_call_time_and_checks_live_growth(
    reader: ModuleType, allowed: Path, capsys
) -> None:
    with patch.object(reader, "_TRANSCRIPT_MAX_BYTES", 4):
        assert reader.read_transcript(str(allowed)) is None
    assert capsys.readouterr().err == DENIAL
    with (
        patch.object(reader, "_TRANSCRIPT_MAX_BYTES", 1024),
        patch.object(
            reader.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=1025)
        ),
    ):
        assert reader.read_transcript(str(allowed)) is None
    assert capsys.readouterr().err == ""
    with (
        patch.object(reader, "_TRANSCRIPT_MAX_BYTES", 4),
        patch.object(
            reader.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=1)
        ),
    ):
        assert reader.read_transcript(str(allowed)) is None
    assert capsys.readouterr().err == ""


def test_readers_preserve_allowed_file_semantics(reader: ModuleType, allowed: Path, capsys) -> None:
    assert reader.read_transcript_full(str(allowed)) == CONTENT
    assert reader.read_transcript_tail(str(allowed), 2) == "match.*\nlast match.*"
    assert reader.read_transcript_from_line(str(allowed), 0) == CONTENT.rstrip("\n")
    assert reader.read_transcript_from_line(str(allowed), 99) == ""
    assert reader.find_last_line_matching(str(allowed), "match") == 3
    assert reader.find_last_line_matching(str(allowed), "regex") is None
    assert capsys.readouterr().err == ""


def test_each_reader_returns_empty_value_for_denied_path(
    reader: ModuleType, tmp_path: Path, capsys
) -> None:
    path = tmp_path / "outside.jsonl"
    path.write_text(CONTENT)
    assert reader.read_transcript_full(str(path)) == ""
    assert reader.read_transcript_tail(str(path)) == ""
    assert reader.read_transcript_from_line(str(path), 1) == ""
    assert reader.find_last_line_matching(str(path), "match") is None
    assert capsys.readouterr().err == DENIAL


def test_denial_trace_is_once_and_never_contains_path(
    reader: ModuleType, tmp_path: Path, capsys
) -> None:
    path = tmp_path / "outside-with-sensitive-name.jsonl"
    path.write_text(CONTENT)
    assert reader.read_transcript(str(path)) is None
    assert reader.read_transcript_full(str(path)) == ""
    stderr = capsys.readouterr().err
    assert stderr == DENIAL
    assert str(path) not in stderr


def test_root_path_itself_is_denied(reader: ModuleType, tmp_path: Path, capsys) -> None:
    root = tmp_path / CLAUDE_ROOT
    root.parent.mkdir(parents=True)
    root.write_text(CONTENT)
    assert_denied(reader, str(root), capsys)


@pytest.mark.parametrize("root", [CLAUDE_ROOT, CODEX_ROOT, COPILOT_ROOT, OMP_ROOT])
def test_default_home_roots_accept_files(
    reader: ModuleType, tmp_path: Path, root: Path, capsys
) -> None:
    path = tmp_path / root / "default.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(CONTENT)
    assert reader.read_transcript(str(path)) == CONTENT
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("env_name", "root"), [("CLAUDE_CONFIG_DIR", CLAUDE_ROOT), ("CODEX_HOME", CODEX_ROOT)]
)
def test_old_default_root_is_denied_after_override(
    reader: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    env_name: str,
    root: Path,
) -> None:
    old_path = tmp_path / root / "old.jsonl"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(CONTENT)
    configured_root = tmp_path / "configured"
    configured = (
        configured_root
        / ("projects" if env_name == "CLAUDE_CONFIG_DIR" else "sessions")
        / "new.jsonl"
    )
    configured.parent.mkdir(parents=True)
    configured.write_text(CONTENT)
    monkeypatch.setenv(env_name, str(configured_root))
    assert_denied(reader, str(old_path), capsys)
    assert reader.read_transcript(str(configured)) == CONTENT
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("env_name", "root"), [("CLAUDE_CONFIG_DIR", CLAUDE_ROOT), ("CODEX_HOME", CODEX_ROOT)]
)
def test_empty_environment_value_falls_back_to_home(
    reader: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    env_name: str,
    root: Path,
) -> None:
    path = tmp_path / root / "fallback.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(CONTENT)
    monkeypatch.setenv(env_name, "")
    assert reader.read_transcript(str(path)) == CONTENT
    assert capsys.readouterr().err == ""


def test_standard_outside_and_special_paths_are_denied(
    reader: ModuleType, tmp_path: Path, capsys
) -> None:
    for path in (
        Path("/etc/passwd"),
        tmp_path / ".gemini/antigravity-cli/conversations/x.pb",
    ):
        assert_denied(reader, str(path), capsys)
    assert_denied(reader, "\x00", capsys)


def test_invalid_utf8_returns_replacement_text(reader: ModuleType, tmp_path: Path, capsys) -> None:
    path = tmp_path / CLAUDE_ROOT / "invalid.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"valid\xfftext")
    assert reader.read_transcript(str(path)) == "valid\ufffdtext"
    assert capsys.readouterr().err == ""


def test_crlf_is_preserved_by_raw_reader_and_split_by_wrappers(
    reader: ModuleType, tmp_path: Path, capsys
) -> None:
    path = tmp_path / CLAUDE_ROOT / "crlf.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"one\r\ntwo\r\n")
    assert reader.read_transcript(str(path)) == "one\r\ntwo\r\n"
    assert reader.read_transcript_from_line(str(path), 1) == "one\ntwo"
    assert capsys.readouterr().err == ""


def test_from_line_positive_start_literals(reader: ModuleType, allowed: Path, capsys) -> None:
    assert reader.read_transcript_from_line(str(allowed), 1) == CONTENT.rstrip("\n")
    assert reader.read_transcript_from_line(str(allowed), 2) == "match.*\nlast match.*"
    assert reader.read_transcript_from_line(str(allowed), 0) == CONTENT.rstrip("\n")
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="/proc/self/fd unavailable")
def test_denials_do_not_leak_file_descriptors(reader: ModuleType, tmp_path: Path) -> None:
    fifo = tmp_path / CLAUDE_ROOT / "leak-check"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    setattr(reader, "_transcript_denial_emitted", True)
    before = len(list(Path("/proc/self/fd").iterdir()))
    for _ in range(50):
        assert reader.read_transcript(str(fifo)) is None
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
