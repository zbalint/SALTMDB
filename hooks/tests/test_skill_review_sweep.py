"""Tests for saltmdb-skill-review-sweep.py hook script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import _saltmdb_hook_common  # noqa: E402


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_exits_zero_when_no_agent_on_path():
    """Verify script exits 0 cleanly when neither 'claude' nor 'codex' is available."""
    sweep = _load_module("saltmdb-skill-review-sweep")
    with patch("shutil.which", return_value=None):
        with patch.object(sys, "exit") as mock_exit:
            sweep.main()
            mock_exit.assert_called_once_with(0)


def test_review_prompt_contains_key_elements():
    """Verify REVIEW_PROMPT contains required 5-step elements, constraints, and threshold."""
    sweep = _load_module("saltmdb-skill-review-sweep")
    prompt = sweep.REVIEW_PROMPT

    # 1. MCP tool mentions
    assert "get_events" in prompt
    assert "store_memory" in prompt

    # 2. Never auto-apply / no file editing constraint
    assert (
        "never auto-apply" in prompt.lower()
        or "no file may ever be edited" in prompt.lower()
        or "do not call any file-editing tool" in prompt.lower()
    )

    # 3. Pairing threshold (20 events)
    assert "20" in prompt


def test_run_quiet_imported_from_common():
    """Verify run_quiet is imported from _saltmdb_hook_common in both sweep scripts."""
    pre_compact = _load_module("saltmdb-pre-compact-sweep")
    skill_review = _load_module("saltmdb-skill-review-sweep")

    assert pre_compact.run_quiet is _saltmdb_hook_common.run_quiet
    assert skill_review.run_quiet is _saltmdb_hook_common.run_quiet
    assert pre_compact.run_quiet.__module__ == "_saltmdb_hook_common"
    assert skill_review.run_quiet.__module__ == "_saltmdb_hook_common"
