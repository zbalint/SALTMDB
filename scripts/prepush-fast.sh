#!/usr/bin/env bash
# Fast pre-push checks, seconds not minutes: secrets in history, workflow lint and workflow audit.
# CI runs this same script in its hygiene job, so a local pass means the same checks pass there.
# Needs gitleaks, actionlint and zizmor on PATH; a missing tool is an error, not a skip.
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in gitleaks actionlint zizmor; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

# The whole history every time: a CI checkout of a push tracks origin/<branch>, so an
# upstream..HEAD range would be empty there. Takes seconds at this repo's size.
gitleaks git --redact --no-banner .
actionlint
# Offline on purpose, so a local run (with or without GH_TOKEN exported) and CI check the same things.
zizmor --no-progress --no-online-audits .github/workflows
