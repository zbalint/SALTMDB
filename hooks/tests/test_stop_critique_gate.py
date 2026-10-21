"""In-process characterization tests for the critique Stop hook state machine."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

HOOK = Path(__file__).resolve().parents[1] / "saltmdb-stop-critique-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("critique_gate_under_test", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _transcript(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / ".claude" / "projects" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(module, home: Path, transcript: Path, session: str = "s") -> tuple[object, int]:
    state = home / ".saltmdb" / "hooks" / ".state"
    state.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": session, "transcript_path": str(transcript)}
    output = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", output),
        patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": ""}),
        patch.object(module.Path, "home", return_value=home),
    ):
        exited = False
        try:
            module.main()
        except SystemExit as exc:
            assert exc.code == 0
            exited = True
        assert exited
    value = json.loads(output.getvalue()) if output.getvalue().strip() else None
    fires_path = state / f"stop-critique-{session}.session_fires"
    fires = int(fires_path.read_text()) if fires_path.exists() else 0
    return value, fires


def test_session_cap_uses_system_random_once(tmp_path):
    module = _load()
    calls = []

    class StubRandom:
        def randint(self, lower, upper):
            calls.append((lower, upper))
            return 3

    cap_path = tmp_path / "cap"
    with patch.object(module.random, "SystemRandom", return_value=StubRandom()):
        assert module.get_or_roll_session_cap(cap_path) == 3
        assert cap_path.read_text() == "3"
        assert module.get_or_roll_session_cap(cap_path) == 3
    assert calls == [(1, 4)]


def test_missing_transcript_exits_without_output(tmp_path):
    module = _load()
    with patch("sys.stdin", io.StringIO('{"session_id":"s"}')):
        exited = False
        try:
            module.main()
        except SystemExit as exc:
            assert exc.code == 0
            exited = True
        assert exited


def test_risky_tool_starts_stage_one_and_rolls_session_fire(tmp_path):
    module = _load()
    transcript = _transcript(
        tmp_path,
        '{"type":"user","message":"do work"}',
        '{"type":"assistant","content":{"name":"Bash"}}',
    )
    result, fires = _run(module, tmp_path, transcript)
    assert result["decision"] == "block"
    assert "saltmdb-stop-critique-prompt" in result["reason"]
    assert fires == 1


def test_stage_one_retry_does_not_consume_another_fire(tmp_path):
    module = _load()
    transcript = _transcript(
        tmp_path,
        '{"type":"user","message":"do work"}',
        '{"type":"assistant","content":{"name":"Bash"}}',
        "<!-- saltmdb-stop-critique-prompt -->",
        '{"type":"assistant","content":{"name":"Bash"}}',
    )
    state = tmp_path / ".saltmdb" / "hooks" / ".state"
    state.mkdir(parents=True)
    (state / "stop-critique-s.count").write_text("1")
    (state / "stop-critique-s.session_fires").write_text("1")
    result, fires = _run(module, tmp_path, transcript)
    assert result["decision"] == "block"
    assert fires == 1


def test_stage_two_requires_lesson_or_prompts_once(tmp_path):
    module = _load()
    transcript = _transcript(
        tmp_path,
        '{"type":"user","message":"do work"}',
        '{"type":"assistant","content":{"name":"Bash"}}',
        "<!-- saltmdb-stop-critique-prompt -->",
        "<!-- saltmdb-self-critique-done -->",
    )
    state = tmp_path / ".saltmdb" / "hooks" / ".state"
    state.mkdir(parents=True)
    (state / "stop-critique-s.count").write_text("1")
    result, _ = _run(module, tmp_path, transcript)
    assert "saltmdb-stop-critique-stage2-prompt" in result["reason"]

    transcript.write_text(
        transcript.read_text() + "<!-- saltmdb-no-lesson-this-turn -->\n", encoding="utf-8"
    )
    result, _ = _run(module, tmp_path, transcript)
    assert result is None


def test_stage_two_accepts_store_memory_and_risky_cap_is_honored(tmp_path):
    module = _load()
    transcript = _transcript(
        tmp_path,
        '{"type":"user","message":"do work"}',
        '{"type":"assistant","content":{"name":"Bash"}}',
        "<!-- saltmdb-stop-critique-prompt -->",
        "<!-- saltmdb-self-critique-done -->",
        '{"name":"store_memory"}',
    )
    state = tmp_path / ".saltmdb" / "hooks" / ".state"
    state.mkdir(parents=True)
    (state / "stop-critique-s.count").write_text("1")
    result, _ = _run(module, tmp_path, transcript)
    assert result is None

    cap = _transcript(
        tmp_path,
        '{"type":"user","message":"do work"}',
        '{"type":"assistant","content":{"name":"Bash"}}',
    )
    (state / "stop-critique-s.session_cap").write_text("1")
    (state / "stop-critique-s.session_fires").write_text("1")
    (state / "stop-critique-s.count").write_text("0")
    result, fires = _run(module, tmp_path, cap, session="s")
    assert result is None
    assert fires == 1
