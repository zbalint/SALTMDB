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
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "ERROR: tracked tree is dirty" >&2
  exit 1
fi
HEAD_SHA="$(git rev-parse HEAD)"
if ! BASE_SHA="$(git rev-parse --verify --quiet refs/remotes/origin/develop)"; then
  echo "ERROR: origin/develop ref is missing" >&2
  exit 1
fi
if [[ "$HEAD_SHA" == "$BASE_SHA" ]]; then
  echo "ERROR: nothing to push; origin/develop equals HEAD" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$BASE_SHA" "$HEAD_SHA"; then
  echo "ERROR: origin/develop is not an ancestor of HEAD" >&2
  exit 1
fi

tests=0
PYTHONPATH=src "$VENV/bin/coverage" run -m pytest tests/ hooks/tests/ || tests=$?
"$VENV/bin/coverage" xml
if [[ "$(git rev-parse HEAD)" != "$HEAD_SHA" ]]; then
  echo "ERROR: HEAD changed while coverage ran" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "ERROR: tracked tree changed while coverage ran" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone --quiet --no-hardlinks --no-checkout "$ROOT" "$TMP/repo"
(
  cd "$TMP/repo"
  git checkout --quiet --detach "$HEAD_SHA"
  git branch -f develop "$BASE_SHA"
  git remote remove origin
)
cp "$ROOT/coverage.xml" "$TMP/repo/coverage.xml"
# A scanner failure must abort before the test status is returned.
(
  cd "$TMP/repo"
  sonar-scanner \
    -Dsonar.branch.name=develop-prepush \
    -Dsonar.branch.target=develop \
    -Dsonar.qualitygate.wait=true
)
exit "$tests"
