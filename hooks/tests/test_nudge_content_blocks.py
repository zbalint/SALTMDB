from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from saltmdb.utils.envelope import ok

HOOKS_DIR = Path(__file__).resolve().parent.parent


def _run(payload: dict, home: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "saltmdb-post-tool-response-nudges.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": "src"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_content_block_list_unwraps_real_near_duplicate_envelope(tmp_path):
    response = ok(
        {
            "memory_id": "memory-1",
            "duplicate_candidates": [{"memory_id": "memory-2"}],
        }
    )
    result = _run(
        {
            "tool_name": "mcp__saltmdb__store_memory",
            "tool_response": [{"type": "text", "text": json.dumps(response)}],
        },
        tmp_path,
    )
    assert "duplicate_candidates" in result["systemMessage"]


def test_unrelated_content_block_list_does_not_nudge(tmp_path):
    result = _run(
        {
            "tool_name": "mcp__saltmdb__store_memory",
            "tool_response": [{"type": "text", "text": json.dumps(ok({"memory_id": "memory-1"}))}],
        },
        tmp_path,
    )
    assert result == {}


def test_oversized_content_block_is_not_unwrapped(tmp_path):
    oversized = ok(
        {
            "memory_id": "memory-1",
            "duplicate_candidates": [{"memory_id": "memory-2"}],
        }
    )
    oversized["padding"] = "x" * (1024 * 1024 + 1)
    oversized_text = json.dumps(oversized)
    result = _run(
        {
            "tool_name": "mcp__saltmdb__store_memory",
            "tool_response": [{"type": "text", "text": oversized_text}],
        },
        tmp_path,
    )
    assert result == {}


def test_dict_response_takes_precedence_over_text_fallback(tmp_path):
    response = ok({"message": "NEAR_DUPLICATE"})
    result = _run(
        {"tool_name": "mcp__saltmdb__store_memory", "tool_response": response},
        tmp_path,
    )
    assert result == {}


def test_raw_string_tool_response_is_parsed(tmp_path):
    response = ok({"duplicate_candidates": ["memory-2"]})
    result = _run(
        {
            "tool_name": "mcp__saltmdb__store_memory",
            "tool_response": json.dumps(response),
        },
        tmp_path,
    )
    assert "duplicate_candidates" in result["systemMessage"]
