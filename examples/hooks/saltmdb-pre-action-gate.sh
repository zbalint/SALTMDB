#!/bin/bash
# SALTMDB Pre-Action Search Gate Hook Script
# Enforces Rule 1 ("Think Before You Leap"): Denies edit/bash tool use until search_memory has been called.

input="$(cat)"

if command -v jq >/dev/null 2>&1; then
  transcript_path="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)"
else
  transcript_path="$(printf '%s' "$input" | grep -o '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
  transcript_path="${transcript_path//\\\\/\\}"
fi

if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  exit 0
fi

if grep -Eq '"name"[[:space:]]*:[[:space:]]*"mcp__saltmdb__search_memory"' "$transcript_path" 2>/dev/null; then
  exit 0
fi

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"SALTMDB Rule 1 (Think Before You Leap): call mcp__saltmdb__search_memory for the relevant component/task before editing files or running commands. This gate fires once per session -- after your first search_memory call, further edits/commands this session go through unblocked."}}
JSON
exit 0
