#!/bin/bash
# SALTMDB Session Bootstrap Hook Script
# Extract CWD from stdin JSON, determine project keywords, and fetch bootstrap digest.

input="$(cat)"

if command -v jq >/dev/null 2>&1; then
  cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)"
else
  cwd="$(printf '%s' "$input" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
fi
[ -z "$cwd" ] && cwd="$PWD"
project_keywords="$(basename "$cwd")"

CLI="$HOME/.mcp/SALTMDB/.venv/bin/saltmdb-cli"
[ -x "$CLI" ] || exit 0

SALTMDB_ENABLE_SEMANTIC=true "$CLI" bootstrap-digest \
  --project-keywords "$project_keywords" 2>/dev/null
exit 0
