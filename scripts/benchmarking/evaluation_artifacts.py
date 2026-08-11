"""Canonical paths and integrity helpers for one frozen evaluation run.

Full matrices, labels, packets, and checkpoints belong below the ignored
``scratch/eval_results/<run-id>/`` tree.  Keeping the names in one place prevents a dev and
blind stage from accidentally sharing an input or writing a supposedly private mapping into a
public packet directory.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

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

