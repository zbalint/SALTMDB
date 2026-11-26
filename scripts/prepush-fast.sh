#!/usr/bin/env bash
# Fast pre-push checks, seconds not minutes: secrets in history, workflow lint, workflow audit,
# repository-local Semgrep invariants, and an advisory CodeScene report.
# CI runs this same script in its hygiene job, so a local pass means the same checks pass there.
# Needs gitleaks, actionlint, zizmor, semgrep, cs (CodeScene) and jq on PATH; a missing tool is an
# error, not a skip. The CodeScene step at the end only reports: it never fails the script.
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in gitleaks actionlint zizmor semgrep cs jq; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

# The whole history every time: a CI checkout of a push tracks origin/<branch>, so an
# upstream..HEAD range would be empty there. Takes seconds at this repo's size.
gitleaks git --redact --no-banner .
actionlint
# Offline on purpose, so a local run (with or without GH_TOKEN exported) and CI check the same things.
zizmor --no-progress --no-online-audits .github
# Each row is the single source of truth for a rule's repository scope. Targets and
# excludes are space-separated relative paths; every exclude is root-relative, never bare.
semgrep_scopes=(
  # SQLite connections are allowed only in the database package.
  "sqlite-connect-outside-db-package|src hooks saltmdb_cli.py saltmdb_server.py|src/saltmdb/db src/saltmdb/models"
  # Shell execution and unsafe deserialization are forbidden in product, hooks and benchmark scripts.
  # The identical scope runs both rule files in one Semgrep invocation.
  "shell-true-or-os-shell,unsafe-deserialization-or-eval|src hooks scripts saltmdb_cli.py saltmdb_server.py|src/saltmdb/models"
  # Daemon/viewer sockets must remain on the loopback literal.
  "network-host-not-loopback-literal|src/saltmdb/daemon src/saltmdb/viewer hooks|src/saltmdb/models"
  # Viewer response bytes must go through send_ helpers.
  "viewer-wfile-write-outside-send-helpers|src/saltmdb/viewer|src/saltmdb/models src/saltmdb/viewer/static/vendor"
  # Product and hooks make no outbound HTTP calls.
  "no-outbound-http-client|src hooks|src/saltmdb/models"
)

semgrep_tmp="$(mktemp -d)"
trap 'rm -rf "$semgrep_tmp"' EXIT
semgrep_pids=()
semgrep_rules=()
semgrep_logs=()

run_semgrep_scope() {
  local scope="$1"
  local log="$2"
  local rules targets excludes
  IFS='|' read -r rules targets excludes <<<"$scope"
  IFS=',' read -r -a rule_args <<<"$rules"
  read -r -a target_args <<<"$targets"
  read -r -a exclude_args <<<"$excludes"
  semgrep_cmd=(semgrep scan)
  for rule in "${rule_args[@]}"; do
    semgrep_cmd+=(--config ".semgrep/rules/${rule}.yml")
  done
  semgrep_cmd+=(
    --metrics=off
    --disable-version-check
    --oss-only
    --error
    --quiet
    --jobs 2
    --include '*.py'
    "${target_args[@]}"
  )
  for exclude in "${exclude_args[@]}"; do
    semgrep_cmd+=(--exclude "$exclude")
  done
  "${semgrep_cmd[@]}" >"$log" 2>&1
}

flush_semgrep_batch() {
  local status
  local semgrep_failed=0
  for i in "${!semgrep_pids[@]}"; do
    set +e
    wait "${semgrep_pids[$i]}"
    status=$?
    set -e
    if (( status != 0 )); then
      echo "Semgrep rules failed: ${semgrep_rules[$i]}" >&2
      cat "${semgrep_logs[$i]}" >&2
      semgrep_failed=1
    fi
  done
  semgrep_pids=()
  semgrep_rules=()
  semgrep_logs=()
  if (( semgrep_failed != 0 )); then
    exit 1
  fi
}

for scope in "${semgrep_scopes[@]}"; do
  rules="${scope%%|*}"
  log="$semgrep_tmp/${#semgrep_pids[@]}-$rules.log"
  echo "Semgrep rules: $rules"
  run_semgrep_scope "$scope" "$log" &
  semgrep_pids+=("$!")
  semgrep_rules+=("$rules")
  semgrep_logs+=("$log")
  if (( ${#semgrep_pids[@]} == 2 )); then
    flush_semgrep_batch
  fi
done
if (( ${#semgrep_pids[@]} != 0 )); then
  flush_semgrep_batch
fi

test_output="$(
  semgrep scan --test --config .semgrep/rules .semgrep/tests \
    --metrics=off --disable-version-check --oss-only --jobs 2 --include '*.py' \
    --exclude src/saltmdb/models --exclude src/saltmdb/viewer/static/vendor 2>&1
)" || {
  printf '%s\n' "$test_output" >&2
  exit 1
}
printf '%s\n' "$test_output"
test_count=0
if [[ "$test_output" =~ ([0-9]+)/[0-9]+: ]]; then
  test_count="${BASH_REMATCH[1]}"
fi
if [[ "$test_count" -eq 0 ]]; then
  echo "Semgrep fixture suite reported zero tests" >&2
  exit 1
fi

# CodeScene code health, advisory: it must never block a push or a CI run. It lists the findings
# the pushed commits introduced or made worse. CODESCENE_BASE overrides the base (CI sets it);
# locally the base is the upstream branch, so the range is the commits about to be pushed.
base="${CODESCENE_BASE:-$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo origin/develop)}"
status=0
# The timeout keeps a stalled licence or network call from cancelling the CI job as failed.
report="$(CS_DISABLE_VERSION_CHECK=1 timeout 300 cs delta "$base" HEAD --output-format json)" || status=$?
if [[ "$status" -eq 0 ]] && findings="$(jq -r '
  .[] | .name as $file | .findings[]
  | select(.["change-type"] == "introduced" or .["change-type"] == "degraded")
  | "\($file): \(.["change-type"]) \(.category): \(.["change-details"][0].description // "")"' <<<"$report")"; then
  summary="CodeScene (advisory) since ${base}: ${findings:-no introduced or degraded findings}"
else
  summary="CodeScene skipped: cs delta against ${base} or reading its output failed (status ${status}); this step is advisory"
fi
printf '%s\n' "$summary"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '%s\n' "$summary" >> "$GITHUB_STEP_SUMMARY"
fi
