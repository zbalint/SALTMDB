"""Black-box tests for scripts/prepush-sonar.sh clone isolation."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = Path(
    os.environ.get("PREPUSH_SONAR_SCRIPT", REPO_ROOT / "scripts" / "prepush-sonar.sh")
)
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@dataclass
class SonarFixture:
    root: Path
    origin: Path
    work: Path
    stub_bin: Path
    record: Path
    env: dict[str, str]

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def run(
        self, *, coverage_status: int = 0, scanner_status: int = 0, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        self.record.unlink(missing_ok=True)
        coverage_record = self.root / "coverage-run-record"
        coverage_record.unlink(missing_ok=True)
        env = {key: value for key, value in self.env.items() if not key.startswith("GIT_")}
        env |= {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": f"{self.stub_bin}{os.pathsep}{os.environ['PATH']}",
            "SONAR_TOKEN": "dummy-token",
            "COVERAGE_RUN_STATUS": str(coverage_status),
            "COVERAGE_RUN_RECORD": str(coverage_record),
            "SONAR_STATUS": str(scanner_status),
            "COVERAGE_MARKER": extra.pop("coverage_marker", "coverage-marker"),
            "SONAR_RECORD": str(self.record),
            **extra,
        }
        return subprocess.run(
            [str(self.work / "scripts" / "prepush-sonar.sh")],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )


def _run_git(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    _ = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def _write_executable(path: Path, body: str) -> None:
    _ = path.write_text(body)
    _ = path.chmod(0o755)


@pytest.fixture
def sonar_fixture(tmp_path: Path) -> SonarFixture:
    root = tmp_path / "sonar fixture with spaces"
    origin = root / "origin.git"
    work = root / "working checkout"
    stub_bin = root / "stubs"
    record = root / "scanner-record.txt"
    root.mkdir()
    stub_bin.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Committer",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )

    _run_git(["init", "--bare", str(origin)], root, env)
    _run_git(["init", "--quiet", "-b", "develop", str(work)], root, env)
    _run_git(["config", "user.name", "Fixture User"], work, env)
    _run_git(["config", "user.email", "fixture@example.invalid"], work, env)
    _ = (work / "scripts").mkdir()
    script = SCRIPT_SOURCE
    _ = shutil.copy2(script, work / "scripts" / script.name)
    _ = (work / "scripts" / script.name).chmod(0o755)
    _ = (work / "sonar-project.properties").write_text("sonar.projectKey=fixture\n")
    _ = (work / ".gitignore").write_text("coverage.xml\n")
    _ = (work / "tracked.txt").write_text("initial\n")
    _run_git(["add", "."], work, env)
    _run_git(["commit", "--quiet", "-m", "initial"], work, env)
    _run_git(["remote", "add", "origin", str(origin)], work, env)
    _run_git(["push", "--quiet", "-u", "origin", "develop"], work, env)

    venv_bin = work / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(
        venv_bin / "coverage",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == run ]]; then
  : > "$COVERAGE_RUN_RECORD"
  if [[ ${MUTATE_TRACKED:-0} == 1 ]]; then
    printf 'mutated\\n' > tracked.txt
  fi
  if [[ ${COMMIT_DURING_COVERAGE:-0} == 1 ]]; then
    printf 'coverage commit\\n' > race.txt
    git add race.txt
    git commit --quiet -m 'coverage race'
  fi
  exit "${COVERAGE_RUN_STATUS:-0}"
fi
if [[ ${1:-} == xml ]]; then
  printf '%s\\n' "${COVERAGE_MARKER:-coverage-marker}" > coverage.xml
  exit 0
fi
exit 2
""",
    )
    _write_executable(
        stub_bin / "sonar-scanner",
        """#!/usr/bin/env bash
set -euo pipefail
{
  printf 'cwd=%s\\n' "$PWD"
  for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
  printf 'head=%s\\n' "$(git rev-parse HEAD)"
  printf 'develop=%s\\n' "$(git rev-parse develop)"
  printf 'remotes=%s\\n' "$(git for-each-ref --format='%(refname)' refs/remotes)"
  if [[ -f coverage.xml ]]; then printf 'coverage=%s\\n' "$(cat coverage.xml)"; else printf 'coverage=missing\\n'; fi
} > "$SONAR_RECORD"
exit "${SONAR_STATUS:-0}"
""",
    )
    return SonarFixture(root, origin, work, stub_bin, record, env)


def _record(fixture: SonarFixture) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in fixture.record.read_text().splitlines():
        key, value = line.split("=", 1)
        values.setdefault(key, []).append(value)
    return values


def _local_commit(fixture: SonarFixture, name: str = "local.txt") -> None:
    _ = (fixture.work / name).write_text("local change\n")
    _ = fixture.git("add", name)
    _ = fixture.git("commit", "--quiet", "-m", "local change")


def test_happy_path_scans_unpushed_commit_in_clean_clone(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)
    head = sonar_fixture.git("rev-parse", "HEAD")
    base = sonar_fixture.git("rev-parse", "refs/remotes/origin/develop")
    result = sonar_fixture.run(coverage_marker="happy-marker")

    assert result.returncode == 0, result.stderr
    values = _record(sonar_fixture)
    assert values["cwd"][0] != str(sonar_fixture.work)
    assert not Path(values["cwd"][0]).exists()
    assert values["head"] == [head]
    assert values["develop"] == [base]
    assert values["head"][0] != values["develop"][0]
    assert values["remotes"] == [""]
    assert values["coverage"] == ["happy-marker"]
    assert values["arg"] == [
        "-Dsonar.branch.name=develop-prepush",
        "-Dsonar.branch.target=develop",
        "-Dsonar.qualitygate.wait=true",
    ]
    assert sonar_fixture.git("rev-parse", "HEAD") == head
    assert sonar_fixture.git("rev-parse", "develop") == head


def test_equal_base_ref_refuses_vacuous_scan(sonar_fixture: SonarFixture) -> None:
    result = sonar_fixture.run()

    assert result.returncode == 1
    assert "nothing to push" in result.stderr
    assert not sonar_fixture.record.exists()


def test_full_remote_ref_ignores_local_origin_develop_shadow(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)
    base = sonar_fixture.git("rev-parse", "refs/remotes/origin/develop")
    _ = sonar_fixture.git("switch", "-c", "shadow", base)
    _ = (sonar_fixture.work / "shadow.txt").write_text("shadow commit\n")
    _ = sonar_fixture.git("add", "shadow.txt")
    _ = sonar_fixture.git("commit", "--quiet", "-m", "shadow commit")
    shadow = sonar_fixture.git("rev-parse", "HEAD")
    _ = sonar_fixture.git("switch", "develop")
    _ = sonar_fixture.git("branch", "origin/develop", shadow)

    result = sonar_fixture.run()

    assert result.returncode == 0, result.stderr
    values = _record(sonar_fixture)
    assert values["develop"] == [base]
    assert values["develop"][0] != shadow


def test_diverged_base_ref_refuses_scan(sonar_fixture: SonarFixture, tmp_path: Path) -> None:
    _local_commit(sonar_fixture)
    other = tmp_path / "other"
    _ = subprocess.run(
        ["git", "clone", "--quiet", str(sonar_fixture.origin), str(other)],
        env=sonar_fixture.env,
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(["config", "user.name", "Other User"], other, sonar_fixture.env)
    _run_git(["config", "user.email", "other@example.invalid"], other, sonar_fixture.env)
    _ = (other / "diverged.txt").write_text("diverged\n")
    _run_git(["add", "diverged.txt"], other, sonar_fixture.env)
    _run_git(["commit", "--quiet", "-m", "diverged"], other, sonar_fixture.env)
    _run_git(["push", "--quiet", "--force", "origin", "HEAD:develop"], other, sonar_fixture.env)
    _ = sonar_fixture.git("fetch", "origin", "develop")

    result = sonar_fixture.run()

    assert result.returncode == 1
    assert "not an ancestor" in result.stderr
    assert not sonar_fixture.record.exists()


def test_dirty_tracked_tree_refuses_but_untracked_file_is_allowed(
    sonar_fixture: SonarFixture,
) -> None:
    _local_commit(sonar_fixture)
    _ = (sonar_fixture.work / "tracked.txt").write_text("dirty\n")
    result = sonar_fixture.run()
    assert result.returncode == 1
    assert "tracked tree" in result.stderr
    assert not sonar_fixture.record.exists()
    assert not (sonar_fixture.root / "coverage-run-record").exists()

    _ = (sonar_fixture.work / "untracked.txt").write_text("allowed\n")
    _ = (sonar_fixture.work / "tracked.txt").write_text("initial\n")
    result = sonar_fixture.run()
    assert result.returncode == 0, result.stderr
    assert sonar_fixture.record.exists()


def test_missing_origin_develop_ref_names_ref_and_skips_scanner(
    sonar_fixture: SonarFixture,
) -> None:
    _local_commit(sonar_fixture)
    _ = sonar_fixture.git("update-ref", "-d", "refs/remotes/origin/develop")

    result = sonar_fixture.run()

    assert result.returncode == 1
    assert "origin/develop" in result.stderr
    assert not sonar_fixture.record.exists()


def test_scanner_failure_is_script_failure(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)

    result = sonar_fixture.run(scanner_status=1)

    assert result.returncode == 1
    assert sonar_fixture.record.exists()


def test_failed_tests_status_survives_successful_scan(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)

    result = sonar_fixture.run(coverage_status=3)

    assert result.returncode == 3
    assert sonar_fixture.record.exists()


def test_scanner_failure_precedes_failed_tests(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)

    result = sonar_fixture.run(coverage_status=3, scanner_status=5)

    assert result.returncode == 5
    assert sonar_fixture.record.exists()


def test_token_is_not_printed(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)

    result = sonar_fixture.run()

    assert result.returncode == 0, result.stderr
    assert "dummy-token" not in result.stdout
    assert "dummy-token" not in result.stderr


def test_head_move_during_coverage_refuses_scan(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)

    result = sonar_fixture.run(COMMIT_DURING_COVERAGE="1")

    assert result.returncode == 1
    assert "HEAD changed" in result.stderr
    assert not sonar_fixture.record.exists()


def test_tracked_change_during_coverage_refuses_scan(sonar_fixture: SonarFixture) -> None:
    _local_commit(sonar_fixture)

    result = sonar_fixture.run(MUTATE_TRACKED="1")

    assert result.returncode == 1
    assert "tracked tree" in result.stderr
    assert not sonar_fixture.record.exists()
