"""Canonical paths and integrity helpers for one frozen evaluation run.

Full matrices, labels, packets, and checkpoints belong below the ignored
``scratch/eval_results/<run-id>/`` tree.  Keeping the names in one place prevents a dev and
blind stage from accidentally sharing an input or writing a supposedly private mapping into a
public packet directory.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Stage-1 artifacts are intentionally content-addressed.  Keeping the provenance contract in
# this small, dependency-free module means the corpus/query/matrix/judging stages cannot drift
# into slightly different ad-hoc fingerprint formats.  The values are metadata only; no runtime
# search behavior is selected here.
PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_KEYS = (
    "commit_fingerprint",
    "corpus_fingerprint",
    "query_manifest_fingerprint",
    "random_seed",
    "config_fingerprint",
    "judge_version_fingerprint",
)


class StaleArtifactError(ValueError):
    """Raised when an artifact was produced from a different evaluation input set."""


def git_commit_fingerprint(repo_root: Path | None = None) -> str:
    """Return the full repository commit hash, or ``"unknown"`` outside a checkout.

    The fallback is explicit rather than silently using a mutable branch name.  Callers that
    need promotion-grade provenance should reject ``unknown`` via :func:`validate_provenance`.
    """
    root = Path(repo_root or Path(__file__).resolve().parents[2])
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def file_fingerprint(path: Path) -> str:
    """SHA-256 fingerprint for a corpus/config/query source file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def machine_fingerprint() -> str:
    """Stable-enough host marker for diagnostic latency artifacts.

    It is deliberately not used in ranking/promotion decisions.  CI can pass an explicit machine
    identifier to :func:`build_provenance` when a stronger host lock is required.
    """
    value = platform.node().strip()
    return hashlib.sha256(value.encode()).hexdigest() if value else "unknown"


def build_provenance(
    *,
    commit_fingerprint: str,
    corpus_fingerprint: str,
    query_manifest_fingerprint: str,
    random_seed: int,
    config_fingerprint: str,
    judge_version_fingerprint: str,
    machine_fingerprint_value: str | None = None,
    artifact_kind: str | None = None,
) -> dict:
    """Build the required reproducibility envelope for a Stage-1 artifact.

    ``random_seed`` is kept as an integer (rather than stringifying it) so two producers cannot
    accidentally sign different JSON representations of the same run.  The optional machine and
    artifact-kind fields are diagnostic labels and do not alter the required input identity.
    """
    values = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "commit_fingerprint": commit_fingerprint,
        "corpus_fingerprint": corpus_fingerprint,
        "query_manifest_fingerprint": query_manifest_fingerprint,
        "random_seed": random_seed,
        "config_fingerprint": config_fingerprint,
        "judge_version_fingerprint": judge_version_fingerprint,
    }
    if machine_fingerprint_value is not None:
        values["machine_fingerprint"] = machine_fingerprint_value
    if artifact_kind is not None:
        values["artifact_kind"] = artifact_kind
    _validate_provenance_shape(values)
    values["fingerprint"] = artifact_fingerprint(values)
    return values


def _validate_provenance_shape(provenance: object) -> None:
    if not isinstance(provenance, dict):
        raise StaleArtifactError("artifact lacks provenance metadata")
    if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise StaleArtifactError("unsupported or stale provenance schema")
    missing = [key for key in PROVENANCE_KEYS if key not in provenance]
    if missing:
        raise StaleArtifactError(f"provenance missing required fields: {', '.join(missing)}")
    if any(provenance[key] in (None, "") for key in PROVENANCE_KEYS if key != "random_seed"):
        raise StaleArtifactError("provenance contains an empty identity field")
    if not isinstance(provenance["random_seed"], int) or isinstance(
        provenance["random_seed"], bool
    ):
        raise StaleArtifactError("provenance random_seed must be an integer")


def validate_provenance(
    artifact: object,
    expected: dict | None = None,
    *,
    artifact_label: str = "artifact",
) -> dict:
    """Validate a provenance envelope and optionally compare it with current run inputs.

    Missing provenance is rejected as stale instead of being silently treated as a compatible
    legacy artifact.  This lets callers explicitly invalidate old artifacts while retaining the
    useful, human-readable failure reason.
    """
    if not isinstance(artifact, dict):
        raise StaleArtifactError(f"{artifact_label} is not an object")
    provenance = artifact.get("provenance")
    _validate_provenance_shape(provenance)
    unsigned = dict(provenance)
    stored = unsigned.pop("fingerprint", None)
    if not isinstance(stored, str) or stored != artifact_fingerprint(unsigned):
        raise StaleArtifactError(f"{artifact_label} provenance fingerprint mismatch")
    if expected is not None:
        for key, expected_value in expected.items():
            if provenance.get(key) != expected_value:
                raise StaleArtifactError(
                    f"{artifact_label} stale provenance: {key} does not match current run"
                )
    return provenance


def with_provenance(artifact: dict, provenance: dict) -> dict:
    """Return a copy of ``artifact`` signed with a validated provenance envelope."""
    _validate_provenance_shape(provenance)
    result = dict(artifact)
    result["provenance"] = dict(provenance)
    result["provenance_fingerprint"] = provenance["fingerprint"]
    return result


PUBLIC_FILES = {
    "corpus_manifest": "corpus_manifest.json",
    "source_slots": "source_slots.json",
    "queries_dev": "queries_dev.json",
    "queries_blind": "queries_blind.json",
    "config_manifest": "config_manifest.json",
    "matrix_dev": "matrix_dev.json",
    "matrix_blind": "matrix_blind.json",
    "analysis_dev": "analysis_dev.json",
    "analysis_blind": "analysis_blind.json",
    "decision": "final_decision.json",
    "provenance": "provenance_manifest.json",
}


def artifact_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_artifact_fingerprint(value: object, *, field: str = "fingerprint") -> None:
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ValueError(f"artifact lacks {field}")
    unsigned = dict(value)
    stored = unsigned.pop(field)
    if stored != artifact_fingerprint(unsigned):
        raise ValueError(f"artifact {field} mismatch")


def run_directory(root: Path, run_id: str) -> Path:
    """Return a validated run directory without allowing path traversal."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be a simple, non-empty filename-safe identifier")
    root = Path(root).resolve()
    path = (root / run_id).resolve()
    if path.parent != root:
        raise ValueError("run_id escapes the evaluation-results root")
    return path


def canonical_artifact_paths(run_dir: Path) -> dict[str, Path]:
    """Return all canonical public and private artifact locations for a run."""
    run_dir = Path(run_dir)
    paths = {name: run_dir / filename for name, filename in PUBLIC_FILES.items()}
    paths.update(
        {
            "public_packets_dev": run_dir / "public_packets" / "dev",
            "public_packets_blind": run_dir / "public_packets" / "blind",
            "private_mappings_dev": run_dir / "private_mappings" / "dev",
            "private_mappings_blind": run_dir / "private_mappings" / "blind",
            "raw_labels_dev": run_dir / "raw_labels" / "dev",
            "raw_labels_blind": run_dir / "raw_labels" / "blind",
            "merged_labels_dev": run_dir / "merged_labels_dev.json",
            "merged_labels_blind": run_dir / "merged_labels_blind.json",
            "arbitration_dev": run_dir / "arbitration_dev.json",
            "arbitration_blind": run_dir / "arbitration_blind.json",
            "checkpoints_dev": run_dir / "checkpoints" / "dev",
            "checkpoints_blind": run_dir / "checkpoints" / "blind",
        }
    )
    return paths


def initialize_run_directory(root: Path, run_id: str) -> dict[str, Path]:
    """Create only the canonical directory skeleton; no evaluation artifact is fabricated."""
    paths = canonical_artifact_paths(run_directory(root, run_id))
    paths["corpus_manifest"].parent.mkdir(parents=True, exist_ok=True)
    for name, path in paths.items():
        if path.suffix == "":
            path.mkdir(parents=True, exist_ok=True)
    return paths
