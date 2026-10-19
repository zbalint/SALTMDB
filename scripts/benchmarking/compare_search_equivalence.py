"""Compare search-equivalence gate artifacts byte-for-byte.

The comparer deliberately preserves JSON object insertion order. It ignores only the four
source-tree provenance fields introduced by the gate amendment and per-call measured latency.
Usage: python compare_search_equivalence.py --baseline BASE --candidate CANDIDATE
       --baseline-sha SHA [--expect-src PATH]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROVENANCE_KEYS = frozenset({"src_realpath", "src_head", "src_status_full", "src_status_src"})
ALLOWLIST: frozenset[str] = frozenset()


def _case_key(case: dict[str, Any]) -> tuple[Any, ...]:
    key = case.get("key")
    if not isinstance(key, list):
        raise ValueError("case key must be a list")
    return tuple(key)


def validate_db_path(path: Path, *, live_path: Path | None = None) -> Path:
    """Apply the same database refusal boundary as the runner."""
    candidate = path.expanduser().resolve()
    home_db = (Path.home() / ".saltmdb").resolve()
    resolved_live = live_path.expanduser().resolve() if live_path is not None else None
    if resolved_live is not None and candidate == resolved_live:
        raise ValueError(f"Refusing to run against the live default DB path ({candidate})")
    if candidate == home_db or home_db in candidate.parents:
        raise ValueError(f"Refusing to run against a path under {home_db}")
    if not candidate.is_file():
        raise ValueError(f"Refusing database path that is not a regular file: {candidate}")
    if candidate.suffix != ".db":
        raise ValueError(f"Refusing database path without .db suffix: {candidate}")
    return candidate


def _validate_artifact(artifact: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    header = artifact.get("header")
    cases = artifact.get("cases")
    liveness = artifact.get("liveness")
    if not isinstance(header, dict):
        errors.append(f"{label} header is missing or not an object")
    if not isinstance(cases, list):
        errors.append(f"{label} cases is missing or not a list")
    if not isinstance(liveness, dict) or not all(
        liveness.get(key) is True for key in ("preflight", "cross_encoder", "preview", "semantic")
    ):
        errors.append(f"{label} liveness checks are not all passing")
    if artifact.get("complete") is not True:
        errors.append(f"{label} artifact is incomplete")
    if isinstance(cases, list):
        seen: set[tuple[Any, ...]] = set()
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                errors.append(f"{label} case {index} is not an object")
                continue
            try:
                key = _case_key(case)
            except ValueError as error:
                errors.append(f"{label} case {index}: {error}")
                continue
            if key in seen:
                errors.append(f"{label} duplicate case {key!r}")
            seen.add(key)
            if "output" not in case or "arguments" not in case:
                errors.append(f"{label} case {key!r} is missing output or arguments")
    return errors


def _compare_value(left: Any, right: Any, path: str, allowlist: frozenset[str]) -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        left_keys = [key for key in left if key not in allowlist]
        right_keys = [key for key in right if key not in allowlist]
        if left_keys != right_keys:
            missing_left = [key for key in left_keys if key not in right_keys]
            missing_right = [key for key in right_keys if key not in left_keys]
            detail = f"missing={missing_left!r}, extra={missing_right!r}"
            return [f"{path}: object key order or membership differs ({detail})"]
        differences: list[str] = []
        for key in left_keys:
            differences.extend(_compare_value(left[key], right[key], f"{path}.{key}", allowlist))
        return differences
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: list length differs ({len(left)} != {len(right)})"]
        differences = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(_compare_value(left_item, right_item, f"{path}[{index}]", allowlist))
        return differences
    if type(left) is not type(right) or left != right:
        return [f"{path}: {left!r} != {right!r}"]
    return []


def _compare_header(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    baseline_sha: str,
    expect_src: str | None,
) -> list[str]:
    errors: list[str] = []
    baseline_header = baseline.get("header", {})
    candidate_header = candidate.get("header", {})
    if baseline_header.get("src_status_src") != "":
        errors.append("dirty baseline source tree under src")
    if baseline_header.get("src_head") != baseline_sha:
        errors.append("baseline SHA does not match --baseline-sha")
    if expect_src is not None:
        expected_path = Path(expect_src).resolve()
        if expected_path.name != "__init__.py":
            expected_path /= "saltmdb/__init__.py"
        if candidate_header.get("src_realpath") != str(expected_path):
            errors.append("candidate src_realpath does not match --expect-src")
    left_environment = baseline_header.get("environment")
    right_environment = candidate_header.get("environment")
    if not isinstance(left_environment, dict) or not isinstance(right_environment, dict):
        errors.append("header.environment mapping is malformed")
        left_environment = {}
        right_environment = {}
    left = {
        key: value
        for key, value in baseline_header.items()
        if key not in PROVENANCE_KEYS and key != "environment"
    }
    right = {
        key: value
        for key, value in candidate_header.items()
        if key not in PROVENANCE_KEYS and key != "environment"
    }
    if left_environment != right_environment:
        errors.append("header.environment mapping differs")
    errors.extend(_compare_value(left, right, "header", frozenset()))
    return errors


def compare_artifacts(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    baseline_sha: str,
    expect_src: str | None = None,
    allowlist: frozenset[str] = ALLOWLIST,
) -> list[str]:
    """Return first-path diagnostics; an empty list means exact equivalence."""
    errors = _validate_artifact(baseline, "baseline") + _validate_artifact(candidate, "candidate")
    if errors:
        return errors
    errors.extend(_compare_header(baseline, candidate, baseline_sha, expect_src))
    errors.extend(
        _compare_value(
            {key: value for key, value in baseline.items() if key not in {"header", "cases"}},
            {key: value for key, value in candidate.items() if key not in {"header", "cases"}},
            "artifact",
            allowlist,
        )
    )
    baseline_keys = [_case_key(case) for case in baseline["cases"]]
    candidate_keys = [_case_key(case) for case in candidate["cases"]]
    if len(set(baseline_keys)) != len(baseline_keys):
        errors.append("baseline duplicate case")
    if len(set(candidate_keys)) != len(candidate_keys):
        errors.append("candidate duplicate case")
    baseline_cases = dict(zip(baseline_keys, baseline["cases"]))
    candidate_cases = dict(zip(candidate_keys, candidate["cases"]))
    missing = sorted(set(baseline_cases) - set(candidate_cases), key=repr)
    extra = sorted(set(candidate_cases) - set(baseline_cases), key=repr)
    if missing:
        errors.append(f"missing case {missing[0]!r}")
    if extra:
        errors.append(f"extra case {extra[0]!r}")
    for key in sorted(set(baseline_cases) & set(candidate_cases), key=repr):
        left = dict(baseline_cases[key])
        right = dict(candidate_cases[key])
        left.pop("elapsed_seconds", None)
        right.pop("elapsed_seconds", None)
        differences = _compare_value(left, right, f"case {key!r}", allowlist)
        if differences:
            errors.append(differences[0])
    return errors


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"artifact {path} must contain an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--expect-src", type=Path)
    args = parser.parse_args(argv)
    try:
        baseline = _load(args.baseline)
        candidate = _load(args.candidate)
        differences = compare_artifacts(
            baseline,
            candidate,
            baseline_sha=args.baseline_sha,
            expect_src=str(args.expect_src) if args.expect_src else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"comparison refused: {error}")
        return 2
    if differences:
        for difference in differences:
            print(difference)
        print(f"{len(differences)} difference(s)")
        return 1
    unchanged = candidate.get("header", {}).get("src_head") == baseline.get("header", {}).get(
        "src_head"
    )
    suffix = " candidate tree is unchanged from baseline" if unchanged else ""
    print(f"0 differences{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
