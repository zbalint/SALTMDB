#!/bin/bash
# SALTMDB GitHub Copilot CLI Pre-Tool Interceptor Hook Script
# Reads tool execution context on stdin and outputs permissionDecision JSON on stdout.

input="$(cat)"
tool_name="$(printf '%s' "$input" | jq -r '.toolName // .tool // empty' 2>/dev/null)"
transcript_path="$(printf '%s' "$input" | jq -r '.transcriptPath // empty' 2>/dev/null)"

if [ -f "$transcript_path" ] && grep -q 'mcp__saltmdb__search_memory' "$transcript_path"; then
  echo '{"permissionDecision": "allow"}'
  exit 0
fi

case "$tool_name" in
  view*|read*|grep*|list*)
    echo '{"permissionDecision": "allow"}'
    exit 0
    ;;
  *)
    echo '{
      "permissionDecision": "deny",
      "permissionDecisionReason": "SALTMDB Policy: Call mcp__saltmdb__search_memory for the target component before modifying code or running terminal commands."
    }'
    exit 0
    ;;
esac
