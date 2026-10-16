#!/usr/bin/env bash
# Local coverage and SonarQube Cloud gate; needs SONAR_TOKEN, sonar-scanner, and the repo .venv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

cd "$ROOT"

if [[ ! -x "$VENV/bin/coverage" ]]; then
  echo "ERROR: coverage not found at $VENV/bin/coverage" >&2
  exit 1
fi
if [[ -z "${SONAR_TOKEN:-}" ]]; then
  echo "ERROR: SONAR_TOKEN is not set" >&2
  exit 1
fi
if ! command -v sonar-scanner >/dev/null; then
  echo "ERROR: sonar-scanner is not on PATH" >&2
  exit 1
fi

tests=0
PYTHONPATH=src "$VENV/bin/coverage" run -m pytest tests/ hooks/tests/ || tests=$?
"$VENV/bin/coverage" xml
sonar-scanner \
  -Dsonar.branch.name=develop-prepush \
  -Dsonar.branch.target=develop \
  -Dsonar.qualitygate.wait=true
exit "$tests"
