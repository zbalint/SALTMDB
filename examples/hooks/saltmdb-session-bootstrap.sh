#!/bin/bash
# SALTMDB Session Bootstrap Hook Script
# Prints the canonical core-memory bootstrap digest. Core-memory bootstrap governance rewrite:
# bootstrap-digest is global and core-only now (no per-project keyword search, no CWD/project-name
# derivation) -- every active core memory counts and is injected, nothing else.

CLI="$HOME/.mcp/SALTMDB/.venv/bin/saltmdb-cli"
[ -x "$CLI" ] || exit 0

"$CLI" bootstrap-digest 2>/dev/null
exit 0
