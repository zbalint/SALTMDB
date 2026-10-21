"""Direct hook entry points must refuse transcripts outside harness-owned roots."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1]
DENIAL = "saltmdb hook: transcript path denied or unreadable\n"
ISSUE = {"event_type": "issue", "error_code": "repeat-failure"}
CLAUDE_ROOT = Path(".claude/projects")


def load_hook(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), HOOKS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    sys.path.insert(0, str(HOOKS))
    import _saltmdb_hook_common as common

    monkeypatch.setattr(common, "_transcript_denial_emitted", False)
    return tmp_path


def transcript(home, allowed, lines):
    path = home / CLAUDE_ROOT / "session.jsonl" if allowed else home / "outside.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def invoke(module, payload, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    try:
        module.main()
    except SystemExit as exc:
        assert exc.code == 0


def configure_trace_capture(module, monkeypatch, captured, *, fail=False):
    monkeypatch.setattr(
        module,
        "_adapter",
        lambda *_args: {
            "agent_id": "copilot",
            "agent_session_id": "adapter-1",
            "db_path": "db",
        },
    )
    monkeypatch.setattr(module, "_daemon", lambda *_args: (1, "token"))

    def capture(*args):
        captured.append(args)
        if fail:
            raise RuntimeError("test capture failure")

    monkeypatch.setattr(module, "_capture", capture)


@pytest.mark.parametrize("allowed", [True, False])
def test_circuit_breaker_transcript_boundary(environment, monkeypatch, capsys, allowed):
    module = load_hook("saltmdb-post-tool-failure-circuit-breaker.py")
    path = transcript(environment, allowed, [ISSUE, ISSUE])
    payload = {
        "tool_name": "mcp__saltmdb__log_event",
        "tool_input": ISSUE,
        "transcript_path": str(path),
    }
    invoke(module, payload, monkeypatch)
    output = capsys.readouterr()
    if allowed:
        assert "circuit breaker" in json.loads(output.out)["systemMessage"]
        assert output.err == ""
    else:
        assert output.out == ""
        assert output.err == DENIAL
        assert str(path) not in output.err


@pytest.mark.parametrize("allowed", [True, False])
def test_response_nudges_transcript_boundary(environment, monkeypatch, capsys, allowed):
    module = load_hook("saltmdb-post-tool-response-nudges.py")
    path = transcript(environment, allowed, [{"name": "store_memory"}])
    invoke(
        module,
        {"tool_name": "mcp__saltmdb__store_memory", "transcript_path": str(path)},
        monkeypatch,
    )
    output = capsys.readouterr()
    if allowed:
        assert "no manage_relation" in json.loads(output.out)["systemMessage"]
        assert output.err == ""
    else:
        assert output.out == ""
        assert output.err == DENIAL
        assert str(path) not in output.err


@pytest.mark.parametrize("allowed", [True, False])
def test_critique_gate_transcript_boundary(environment, monkeypatch, capsys, allowed):
    module = load_hook("saltmdb-stop-critique-gate.py")
    path = transcript(environment, allowed, [{"type": "user"}, {"name": "Bash"}])
    invoke(module, {"session_id": "direct-sites", "transcript_path": str(path)}, monkeypatch)
    output = capsys.readouterr()
    if allowed:
        assert json.loads(output.out)["decision"] == "block"
        assert output.err == ""
    else:
        assert output.out == ""
        assert output.err == DENIAL
        assert str(path) not in output.err


@pytest.mark.parametrize("allowed", [True, False])
def test_critique_user_reader_boundary(environment, capsys, allowed):
    module = load_hook("saltmdb-stop-critique-gate.py")
    path = transcript(environment, allowed, [{"type": "user"}])
    assert module.find_last_user_line(str(path)) == (1 if allowed else None)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ("" if allowed else DENIAL)


@pytest.mark.parametrize("allowed", [True, False])
def test_critique_line_count_boundary(environment, capsys, allowed):
    module = load_hook("saltmdb-stop-critique-gate.py")
    path = transcript(environment, allowed, [{"type": "user"}, {"name": "Bash"}])
    assert module._line_count(str(path)) == (2 if allowed else 0)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ("" if allowed else DENIAL)


def test_trace_capture_transcript_boundary(environment, monkeypatch):
    module = load_hook("saltmdb-stop-trace-capture.py")
    allowed = environment / ".copilot" / "session-state" / "cs-1" / "events.jsonl"
    allowed.parent.mkdir(parents=True)
    allowed.write_text(
        json.dumps({"type": "user.message", "data": {"content": "hello", "interactionId": "i-1"}})
    )
    outside = environment / "outside-events.jsonl"
    outside.write_text(allowed.read_text())
    captured = []
    monkeypatch.setattr(
        module,
        "_adapter",
        lambda *_args: {
            "agent_id": "copilot",
            "agent_session_id": "adapter-1",
            "db_path": "db",
        },
    )
    monkeypatch.setattr(module, "_daemon", lambda *_args: (1, "token"))

    def capture(*args):
        captured.append(args)
        if args[3] == "<invalid>":
            raise RuntimeError("test capture failure")

    monkeypatch.setattr(module, "_capture", capture)

    assert (
        module.run({"sessionId": "cs-1", "transcriptPath": str(allowed)}, environment, {}, 1) == 0
    )
    assert captured
    assert captured[0][3] == "cs-1"
    captured.clear()
    assert (
        module.run({"sessionId": "a\nb", "transcriptPath": str(allowed)}, environment, {}, 1) == 0
    )
    assert captured[0][3] == "<invalid>"
    captured.clear()
    assert (
        module.run({"sessionId": "cs-1", "transcriptPath": str(outside)}, environment, {}, 1) == 0
    )
    assert not captured
    log = (environment / ".saltmdb/hooks/trace-capture.log").read_text()
    assert "session <invalid>" in log
    assert "a\nb" not in log
    assert "transcript path denied or unreadable" in log
    assert str(outside) not in log


@pytest.mark.parametrize("session_id", ["../escape", "/absolute", "line\nbreak", "", 42])
def test_trace_capture_rejects_invalid_default_session_ids(environment, session_id, monkeypatch):
    module = load_hook("saltmdb-stop-trace-capture.py")
    monkeypatch.setattr(
        module,
        "_adapter",
        lambda *_args: {
            "agent_id": "copilot",
            "agent_session_id": "adapter-1",
            "db_path": "db",
        },
    )
    monkeypatch.setattr(module, "_daemon", lambda *_args: (1, "token"))
    captured = []
    monkeypatch.setattr(module, "_capture", lambda *args: captured.append(args))

    assert module.run({"sessionId": session_id}, environment, {}, 1) == 0
    assert not captured
    log = (environment / ".saltmdb/hooks/trace-capture.log").read_text()
    assert "session <invalid>" in log
    if session_id:
        assert str(session_id) not in log


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_circuit_breaker_denied_missing_or_non_file_transcript(
    environment, monkeypatch, capsys, path_kind
):
    module = load_hook("saltmdb-post-tool-failure-circuit-breaker.py")
    path = environment / "missing.jsonl"
    if path_kind == "directory":
        path.mkdir()
    invoke(
        module,
        {
            "tool_name": "mcp__saltmdb__log_event",
            "tool_input": ISSUE,
            "transcript_path": str(path),
        },
        monkeypatch,
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == DENIAL


@pytest.mark.parametrize("session_id", [".", ".."])
def test_trace_capture_rejects_dot_default_ids_without_reading(
    environment, monkeypatch, session_id
):
    module = load_hook("saltmdb-stop-trace-capture.py")
    events = environment / ".copilot" / "session-state" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps({"type": "user.message", "data": {"content": "read", "interactionId": "i-1"}})
    )
    captured = []
    configure_trace_capture(module, monkeypatch, captured)

    assert module.run({"sessionId": session_id}, environment, {}, 1) == 0
    assert not captured
    log = (environment / ".saltmdb/hooks/trace-capture.log").read_text()
    assert "session <invalid>" in log


@pytest.mark.parametrize("length", [128, 129])
def test_trace_capture_default_session_id_length_boundary(environment, monkeypatch, length):
    module = load_hook("saltmdb-stop-trace-capture.py")
    session_id = "a" * length
    events = environment / ".copilot" / "session-state" / session_id / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps({"type": "user.message", "data": {"content": "read", "interactionId": "i-1"}})
    )
    captured = []
    configure_trace_capture(module, monkeypatch, captured)

    assert module.run({"sessionId": session_id}, environment, {}, 1) == 0
    if length == 128:
        assert captured
        assert captured[0][3] == session_id
    else:
        assert not captured
        log = (environment / ".saltmdb/hooks/trace-capture.log").read_text()
        assert "session <invalid>" in log


def test_trace_capture_valid_default_id_log_label(environment, monkeypatch):
    module = load_hook("saltmdb-stop-trace-capture.py")
    events = environment / ".copilot" / "session-state" / "cs-1" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps({"type": "user.message", "data": {"content": "read", "interactionId": "i-1"}})
    )
    captured = []
    configure_trace_capture(module, monkeypatch, captured, fail=True)

    assert module.run({"sessionId": "cs-1"}, environment, {}, 1) == 0
    log = (environment / ".saltmdb/hooks/trace-capture.log").read_text()
    assert "session cs-1:" in log


def test_trace_capture_replaces_invalid_utf8_inside_allowed_events(environment, monkeypatch):
    module = load_hook("saltmdb-stop-trace-capture.py")
    events = environment / ".copilot" / "session-state" / "cs-1" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_bytes(
        b'{"type":"user.message","data":{"content":"bad \x80 byte","interactionId":"i-1"}}'
    )
    captured = []
    configure_trace_capture(module, monkeypatch, captured)

    assert module.run({"sessionId": "cs-1"}, environment, {}, 1) == 0
    assert "\ufffd" in captured[0][4][0]["messages"][0]
