import json
import os
import sys
import time
from pathlib import Path

import pytest

from saltmdb.viewer.tool_runner import ToolRunner, ToolRunnerError

FIXTURE = Path(__file__).parent / "fixtures" / "viewer_tools_server.py"


def test_real_stdio_discovery_and_call_use_neutral_cwd_and_viewer_identity(tmp_path):
    runner = ToolRunner(
        str(tmp_path / "viewer.db"),
        command=[sys.executable, str(FIXTURE.resolve())],
        env={**os.environ, "VIEWER_TOOLS_JOURNAL": str(tmp_path / "calls.jsonl")},
    )
    try:
        tools = runner.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert set(by_name) == {"search_tags", "store_memory", "mystery_tool"}
        assert by_name["store_memory"].inputSchema["required"] == ["content"]
        result = runner.call_tool("search_tags", {})
        assert result.isError is False
        data = json.loads(result.content[0].text)
        assert data["tags"] == ["fixture"]
        assert data["cwd"] == str(tmp_path)
        assert data["agent_id"] == "viewer"
    finally:
        runner.close()


def _journal(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def _runner(tmp_path, **kwargs):
    return ToolRunner(
        str(tmp_path / "viewer.db"),
        command=[sys.executable, str(FIXTURE.resolve())],
        env={**os.environ, "VIEWER_TOOLS_JOURNAL": str(tmp_path / "calls.jsonl")},
        **kwargs,
    )


def test_tool_errors_timeout_and_timeout_restart(tmp_path):
    journal = tmp_path / "calls.jsonl"
    runner = _runner(tmp_path, call_timeout_s=0.15)
    try:
        result = runner.call_tool("search_tags", {"mode": "raise"})
        assert result.isError is True
        first_pid = _journal(journal)[-1]["pid"]
        with pytest.raises(ToolRunnerError, match="timed out") as error:
            runner.call_tool("search_tags", {"delay": 1})
        assert error.value.kind == "timeout"
        _wait_until(lambda: not Path(f"/proc/{first_pid}").exists())
        result = runner.call_tool("search_tags", {})
        assert result.isError is False
        assert _journal(journal)[-1]["pid"] != first_pid
    finally:
        runner.close()


def test_killed_adapter_restarts_before_next_call(tmp_path):
    journal = tmp_path / "calls.jsonl"
    runner = _runner(tmp_path)
    try:
        runner.call_tool("search_tags", {})
        first_pid = _journal(journal)[-1]["pid"]
        os.kill(first_pid, 9)
        _wait_until(lambda: not Path(f"/proc/{first_pid}").exists())
        result = runner.call_tool("search_tags", {})
        assert result.isError is False
        assert _journal(journal)[-1]["pid"] != first_pid
    finally:
        runner.close()


def test_idle_close_restarts_and_close_is_permanent(tmp_path):
    journal = tmp_path / "calls.jsonl"
    runner = _runner(tmp_path, idle_timeout_s=0.1)
    runner.call_tool("search_tags", {})
    first_pid = _journal(journal)[-1]["pid"]
    _wait_until(lambda: not Path(f"/proc/{first_pid}").exists())
    try:
        result = runner.call_tool("search_tags", {})
        assert result.isError is False
        second_pid = _journal(journal)[-1]["pid"]
        assert second_pid != first_pid
    finally:
        runner.close()
    _wait_until(lambda: not Path(f"/proc/{second_pid}").exists())
    with pytest.raises(ToolRunnerError, match="closed"):
        runner.list_tools()
