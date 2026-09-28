import json
import subprocess
import unittest
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parents[1]
POST_TOOL_MATCHER = (
    "mcp__saltmdb__store_memory|mcp__saltmdb__revise_memory|"
    "mcp__saltmdb__supersede_memory|mcp__saltmdb__consolidate_memories"
)


class TestCaptureHookConfig(unittest.TestCase):
    def _settings(self, name):
        with (HOOKS_DIR / name).open(encoding="utf-8") as handle:
            return json.load(handle)

    def _capture_entry(self, settings, event, tool):
        entries = [
            hook
            for group in settings["hooks"].get(event, [])
            for hook in group["hooks"]
            if hook.get("type") == "mcp_tool" and hook.get("tool") == tool
        ]
        self.assertEqual(len(entries), 1, (event, tool))
        entry = entries[0]
        self.assertEqual(entry["server"], "saltmdb")
        self.assertEqual(entry["timeout"], 15)
        return entry

    def test_both_harnesses_register_exact_capture_entries(self):
        for filename, harness, turn_field in (
            ("claude-settings-example.json", "claude_code", "prompt_id"),
            ("codex-settings-example.json", "codex", "turn_id"),
        ):
            settings = self._settings(filename)
            start = self._capture_entry(settings, "UserPromptSubmit", "capture_trace_start")
            link = self._capture_entry(settings, "PostToolUse", "capture_trace_memory_link")
            complete = self._capture_entry(settings, "Stop", "capture_trace_complete")

            self.assertEqual(start["input"], {
                "harness": harness,
                "harness_session_id": "${session_id}",
                "harness_turn_id": f"${{{turn_field}}}",
                "user_prompt": "${prompt}",
            })
            self.assertEqual(link["input"], {
                "harness_turn_id": f"${{{turn_field}}}",
                "entity_id": "${tool_response.data.id}",
                "just_run_tool_name": "${tool_name}",
            })
            self.assertEqual(complete["input"], {
                "harness_turn_id": f"${{{turn_field}}}",
                "final_assistant_message": "${last_assistant_message}",
            })
            link_groups = [
                group
                for group in settings["hooks"]["PostToolUse"]
                if any(hook is link for hook in group["hooks"])
            ]
            self.assertEqual(len(link_groups), 1)
            self.assertEqual(link_groups[0]["matcher"], POST_TOOL_MATCHER)

    def test_claude_pre_existing_entries_remain_byte_identical(self):
        settings = self._settings("claude-settings-example.json")
        baseline_text = subprocess.run(
            ["git", "show", "HEAD:hooks/claude-settings-example.json"],
            cwd=HOOKS_DIR.parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        baseline = json.loads(baseline_text)
        for event in ("SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"):
            before = baseline["hooks"].get(event, [])
            after = settings["hooks"].get(event, [])
            capture_count = sum(
                hook.get("type") == "mcp_tool"
                for group in after
                for hook in group["hooks"]
            )
            before_commands = [
                hook
                for group in before
                for hook in group["hooks"]
                if hook.get("type") == "command"
            ]
            after_commands = [
                hook
                for group in after
                for hook in group["hooks"]
                if hook.get("type") == "command"
            ]
            self.assertEqual(after_commands, before_commands)
            self.assertEqual(capture_count, 1 if event in {"PostToolUse", "Stop"} else 0)


if __name__ == "__main__":
    unittest.main()
