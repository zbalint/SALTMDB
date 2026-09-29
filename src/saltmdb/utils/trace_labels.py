"""Labelling of Claude Code background-task completion notices captured as trace prompts.

The harness delivers a finished background task to the agent as a user-role turn, so the
UserPromptSubmit hook records it as a trace whose prompt is raw ``<task-notification>`` XML.
Capture rewrites that prompt to a short labelled line; the session handover recognises the
label (and the legacy raw XML already stored) to keep the real user request in view.
"""

import re

TASK_NOTIFICATION_TAG = "<task-notification>"
BACKGROUND_TASK_LABEL = "[background task"
# Prefixes of a stored prompt that is a background-task notice: the label written now, and the
# raw XML of traces captured before the label existed (no backfill of those rows).
BACKGROUND_TASK_PROMPT_PREFIXES = (BACKGROUND_TASK_LABEL, TASK_NOTIFICATION_TAG)

_STATUS = re.compile(r"<status>(.*?)</status>", re.DOTALL)
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
# 'Background command "<description>" completed (exit code 0)': the quoted description is
# greedy so it may itself contain quotes; the trailer after the last quote carries the outcome.
_SUMMARY_DESCRIPTION = re.compile(r'^[^"]*"(.*)"([^"]*)$', re.DOTALL)
_EXIT_CODE = re.compile(r"exit code (\d+)")


def label_task_notification(prompt: str) -> str:
    """``[background task <status>] <description>`` for a notification prompt; any other
    prompt is returned unchanged. An unrecognised summary shape is shown whole, and a
    notification with no summary keeps its raw text under the bare label, so nothing is lost."""
    if not prompt.startswith(TASK_NOTIFICATION_TAG):
        return prompt
    status = _STATUS.search(prompt)
    label = (
        f"{BACKGROUND_TASK_LABEL} {status.group(1).strip()}]"
        if status
        else BACKGROUND_TASK_LABEL + "]"
    )
    summary = _SUMMARY.search(prompt)
    if summary is None:
        return f"{label} {prompt}"
    text = summary.group(1).strip()
    described = _SUMMARY_DESCRIPTION.match(text)
    if described is None:
        return f"{label} {text}"
    exit_code = _EXIT_CODE.search(described.group(2))
    suffix = f" (exit {exit_code.group(1)})" if exit_code and exit_code.group(1) != "0" else ""
    return f"{label} {described.group(1)}{suffix}"
