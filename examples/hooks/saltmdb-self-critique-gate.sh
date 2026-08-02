#!/bin/bash
# SALTMDB Stop Self-Critique Gate Hook Script
# Triggers mandatory 2-question self-reflection before closing turns that involved code or file modifications.

input="$(cat)"
MARKER="saltmdb-self-critique-done"
PROMPT_SENTINEL="saltmdb-self-critique-prompt"
STATE_DIR="$HOME/.claude/hooks/.state"
mkdir -p "$STATE_DIR" 2>/dev/null

# Best-effort cleanup of stale per-session state files
find "$STATE_DIR" -maxdepth 1 -name 'stop-critique-*.count' -mtime +2 -delete 2>/dev/null

extract_field() {
  field="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$input" | jq -r --arg f "$field" '.[$f] // empty' 2>/dev/null
  else
    val="$(printf '%s' "$input" | grep -o "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
    printf '%s' "${val//\\\\/\\}"
  fi
}

transcript_path="$(extract_field transcript_path)"
session_id="$(extract_field session_id)"

if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  exit 0
fi

state_file="$STATE_DIR/stop-critique-${session_id:-unknown}.count"
last_user_line="$(grep -n '"type"[[:space:]]*:[[:space:]]*"user"' "$transcript_path" | tail -1 | cut -d: -f1)"
last_prompt_line="$(grep -n "$PROMPT_SENTINEL" "$transcript_path" | tail -1 | cut -d: -f1)"

if [ -n "$last_prompt_line" ]; then
  window_start="$((last_prompt_line + 1))"
elif [ -n "$last_user_line" ]; then
  window_start="$last_user_line"
fi

if [ -n "$window_start" ]; then
  segment="$(tail -n +"$window_start" "$transcript_path")"
else
  segment="$(tail -n 400 "$transcript_path")"
fi

if [ -n "$last_prompt_line" ] && printf '%s' "$segment" | grep -q "$MARKER"; then
  rm -f "$state_file" 2>/dev/null
  exit 0
fi

if ! printf '%s' "$segment" | grep -Eq '"name"[[:space:]]*:[[:space:]]*"(Edit|Write|NotebookEdit|Bash|PowerShell)"'; then
  exit 0
fi

count="$(cat "$state_file" 2>/dev/null || echo 0)"
case "$count" in ''|*[!0-9]*) count=0 ;; esac

if [ "$count" -ge 2 ]; then
  rm -f "$state_file" 2>/dev/null
  exit 0
fi

echo $((count + 1)) > "$state_file"

cat <<JSON
{"decision":"block","reason":"<!-- $PROMPT_SENTINEL --> Before finishing, answer two questions about the work you just did this turn: (1) What are you least confident about in what you just did? (2) What's the biggest thing about this you probably haven't thought to ask? Begin your reply with the exact line <!-- $MARKER --> (so this check does not re-trigger), then answer both questions concisely."}
JSON
exit 0
