"""Assemble and sign one Gate-D ``BakeoffSpec`` for the search-accuracy retrieval bakeoff.

This is the final control artifact before any retrieval cell runs: it binds the frozen Gate-A
``ModelLock``s, the Gate-B ``QuerySlotAssignments``, and the frozen Gate-A-adjacent corpus
manifest into one signed, content-addressed declaration that ``bakeoff_orchestrator.py``'s
``init`` action freezes as the immutable root of one bakeoff run.  Every field below is either
read directly out of an already-signed upstream artifact or derived by a small, documented,
pre-registered rule -- nothing here is invented after the fact.

Field-by-field derivation
--------------------------
``run_id``
    Caller-supplied, filename-safe (see ``_RUN_ID_RE`` below, which must exactly match
    ``bakeoff_orchestrator._SAFE_RUN_ID`` since it becomes a run-directory name later).

``commit``
    ``bakeoff_state`` requires a 64-hex SHA-256, but ``evaluation_artifacts.
    git_commit_fingerprint()`` returns a raw 40-hex git SHA-1.  This field is therefore
    ``sha256(git_commit_fingerprint().encode()).hexdigest()`` -- a hash *of* the commit hash, not
    the commit hash itself.  Any downstream reader that wants to compare this field against a
    real git SHA must apply the same wrapping, not compare directly.

``corpus_snapshot_hash``
    Read verbatim out of the signed ``CorpusRepresentationManifest``'s own ``corpus_root_hash``
    (never hardcoded), via ``bakeoff_state.validate_corpus_manifest``.

``query_slots_hash``
    ``QuerySlotAssignments`` itself carries no ``corpus_root_hash`` field (only sealed dev/blind
    slot metadata).  The binding to the corpus manifest is instead established through
    ``build_query_slots.py``'s private ``--slots-out`` sidecar file, which does declare
    ``corpus_root_hash`` plus a fingerprint over its own slot rows.  ``derive_query_slots_hash``
    below verifies, in order: the sidecar's own fingerprint against its slot content, the
    sidecar's ``corpus_root_hash`` against the supplied manifest, and that re-sealing the
    sidecar's slots (via ``build_query_slots.build_query_slot_assignments`` -- reused, not
    reimplemented) reproduces exactly the supplied ``QuerySlotAssignments`` fingerprint.  The
    field is the resulting ``QuerySlotAssignments["artifact_fingerprint"]``.

``query_prompt_hash``
    ``build_query_slots.GENERATION_PROMPT_HASH``, imported directly, never recomputed.

``rubric_hash``
    ``judge_pool.judge_version_fingerprint()``, called directly.  Its hashing convention is
    ``bakeoff_state.fingerprint``'s convention exactly, so this is a legitimate direct reuse.

``configuration_hash``
    ``bakeoff_state.fingerprint`` over a canonical dict of the exact contenders list (each with
    ``{contender_id, kind, channel, model_lock_source_repository}``) plus the hyperparameter
    grid, so a later ``RetrievalRunBundle`` can prove it was produced under the exact declared
    configuration.

``seeds``
    A small, pre-declared, arbitrary-but-fixed dict of positive ints -- picked now, before any
    result exists, and never re-rolled after seeing results:
    ``{"split": 7, "bootstrap": 11, "judge_shuffle": 13}``.  ``split`` seeds any residual
    randomized-but-deterministic slot/family assignment step, ``bootstrap`` seeds the paired
    family-bootstrap confidence interval computation used by ``PromotionDecision``, and
    ``judge_shuffle`` seeds ``judge_pool.py``'s ``random.Random(_seed(base_seed, ...))`` calls
    that shuffle judge task order.

``machine_fingerprint``
    ``evaluation_artifacts.machine_fingerprint()``, called directly.

``contenders``
    One string identifier per executable ``run_retrieval_bakeoff.py`` cell: the 8 dense
    ``ModelLock``s at the production-relevant entity channel (``dense:<logical_model_id>:
    entity``), the 1 late-interaction ``ModelLock`` (``late_interaction:<logical_model_id>:
    entity``), and the lexical BM25 baseline being challenged (``lexical:bm25``) -- 10 total.
    Chunk-channel variants are deliberately not added: the task's derivation plan calls for the
    production-relevant entity granularity only, and no prior decision in this project's memory
    requires doubling the matrix with a chunk variant per dense model.

``hyperparameter_grids``
    ``run_retrieval_bakeoff.py``'s executable cells (``execute_dense_cell``/``execute_late_cell``/
    ``execute_lexical_cell``) have no numeric tuning knob today: each cell is fully pinned by its
    ``ModelLock`` plus ``kind`` plus ``channel``, with no fusion or rerank axis in the code that
    runs it.  Rather than fabricate an axis that does not exist in the executable cell, this
    declares an honest single-value placeholder grid: ``{"retrieval_cell_variant":
    ["single_pinned_configuration"]}``.

``required_metrics``
    ``list(bakeoff_state.REQUIRED_METRICS)`` verbatim.

``software_versions``
    Real installed versions read from this interpreter's own environment via
    ``importlib.metadata`` (never invented): ``python`` (``platform.python_version()``, which
    reports exactly what ``.venv/bin/python --version`` would for the venv interpreter this
    script runs under), ``fastembed`` and ``onnxruntime`` (the model-execution runtime
    ``retrieval_adapters.py``'s ``fastembed_dense_factory``/``fastembed_late_interaction_factory``
    depend on -- ``onnxruntime`` is fastembed's own execution backend rather than a direct import
    of these two scripts, but it is execution-critical and is therefore pinned here too), and
    ``numpy`` (imported directly by ``retrieval_adapters.py`` for vector validation).

``holm_comparison_family``
    **Deviation from the literal example in the task's derivation plan**, documented here as
    instructed: the plan's example is "one comparison per non-baseline contender vs the lexical
    baseline."  That does not match how ``bakeoff_state`` actually reaches the blind/promotion
    stage: exactly one contender (the signed development winner) is ever unlocked for blind
    execution and a ``PromotionDecision`` -- the other nine contenders are only ever compared
    during *development* selection, which is not Holm-corrected and is not part of this frozen
    family.  The only genuinely blind-stage comparisons are "the single winner vs the lexical
    baseline," repeated once per accuracy metric in ``PromotionDecision.accuracy_deltas``
    (``ndcg_at_10``, ``grade2_recall_at_20``, ``same_specific_fact_grade2_top1``) -- a real
    3-comparison multiple-testing family that Holm correction is actually protecting.  This
    declares exactly those three, including the hard-required
    ``"winner_vs_baseline_same_specific_fact"`` string:
    ``["winner_vs_baseline_same_specific_fact", "winner_vs_baseline_ndcg_at_10",
    "winner_vs_baseline_grade2_recall_at_20"]``.

``split_targets``
    ``{"dev": 400, "blind": 800}`` exactly, per the frozen contract.

``facet_targets``
    ``build_query_slots.FACET_TARGETS`` verbatim -- already exactly matching the shape
    ``bakeoff_state.validate_bakeoff_spec`` requires.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_state import (  # noqa: E402
    REQUIRED_METRICS,
    BakeoffContractError,
    fingerprint,
    sign_artifact,
    validate_bakeoff_spec,
    validate_corpus_manifest,
    validate_model_lock,
    validate_query_slot_assignments,
)
from build_evaluation_queries import artifact_fingerprint as slot_artifact_fingerprint  # noqa: E402
from build_query_slots import (  # noqa: E402
    FACET_TARGETS,
    GENERATION_PROMPT_HASH,
    build_query_slot_assignments,
)
from evaluation_artifacts import git_commit_fingerprint, machine_fingerprint  # noqa: E402
from judge_pool import judge_version_fingerprint  # noqa: E402
from materialize_model_locks import PINNED_MODELS, model_slug  # noqa: E402
from retrieval_architecture import candidate_by_model_id  # noqa: E402


class BakeoffSpecBuildError(ValueError):
    """The Gate-D BakeoffSpec cannot be assembled from the supplied frozen inputs."""


# Must exactly match bakeoff_orchestrator._SAFE_RUN_ID: run_id becomes a run-directory name once
# bakeoff_orchestrator.py's ``init`` action freezes this spec.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

SPLIT_TARGETS: dict[str, int] = {"dev": 400, "blind": 800}

# Arbitrary-but-fixed, picked before any development or blind result exists.  See the module
# docstring's "seeds" section for what each key seeds.  Never re-roll these after seeing results.
DEFAULT_SEEDS: dict[str, int] = {"split": 7, "bootstrap": 11, "judge_shuffle": 13}

LEXICAL_CONTENDER_ID = "lexical:bm25"

# See the module docstring's "holm_comparison_family" section for why this differs from the task
# derivation plan's literal per-contender example.
HOLM_COMPARISON_FAMILY: tuple[str, ...] = (
    "winner_vs_baseline_same_specific_fact",
    "winner_vs_baseline_ndcg_at_10",
    "winner_vs_baseline_grade2_recall_at_20",
)

# Executable retrieval cells (run_retrieval_bakeoff.py) have no numeric tuning knob today; see
# the module docstring's "hyperparameter_grids" section.
HYPERPARAMETER_GRIDS: dict[str, list[str]] = {
    "retrieval_cell_variant": ["single_pinned_configuration"],
}

_SOFTWARE_PACKAGES: tuple[str, ...] = ("fastembed", "onnxruntime", "numpy")


def commit_hash_field(repo_root: Path | None = None) -> str:
    """Wrap the raw 40-hex git SHA-1 into the 64-hex SHA-256 ``bakeoff_state`` requires.

    See the module docstring's "commit" section: this is sha256-of-the-git-sha1, not the raw
    commit hash.
    """
    commit = git_commit_fingerprint(repo_root)
    if commit == "unknown":
        raise BakeoffSpecBuildError(
            "git commit fingerprint is unavailable ('unknown'); refusing to sign a spec with no "
            "traceable commit"
        )
    return hashlib.sha256(commit.encode("utf-8")).hexdigest()


def load_corpus_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and validate the signed CorpusRepresentationManifest; never hardcode its hash."""
    return validate_corpus_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))


def derive_query_slots_hash(
    slots_path: Path, assignments_path: Path, corpus_manifest: Mapping[str, Any]
) -> str:
    """Bind a signed ``QuerySlotAssignments`` to the frozen corpus manifest.

    See the module docstring's "query_slots_hash" section for why the private slots sidecar is
    the binding vehicle.  Any mismatch anywhere in this chain is a hard failure.
    """
    slots_doc = json.loads(slots_path.read_text(encoding="utf-8"))
    if not isinstance(slots_doc, dict) or slots_doc.get("schema_version") != 1:
        raise BakeoffSpecBuildError("query slots sidecar has an unsupported or missing schema")
    slots = slots_doc.get("slots")
    if not isinstance(slots, list) or not slots:
        raise BakeoffSpecBuildError("query slots sidecar has no assigned slots")
    if slots_doc.get("fingerprint") != slot_artifact_fingerprint(slots):
        raise BakeoffSpecBuildError(
            "query slots sidecar fingerprint does not match its own recorded slot content"
        )
    if slots_doc.get("corpus_root_hash") != corpus_manifest["corpus_root_hash"]:
        raise BakeoffSpecBuildError(
            "query slots sidecar is bound to a different corpus_root_hash than the supplied "
            "corpus manifest"
        )
    assignments = validate_query_slot_assignments(
        json.loads(assignments_path.read_text(encoding="utf-8"))
    )
    recomputed = build_query_slot_assignments(slots)
    if recomputed["artifact_fingerprint"] != assignments["artifact_fingerprint"]:
        raise BakeoffSpecBuildError(
            "supplied QuerySlotAssignments does not match the slots sidecar it claims to seal"
        )
    return assignments["artifact_fingerprint"]


def load_contenders(
    model_locks_dir: Path, pinned_models: Sequence[Any] = PINNED_MODELS
) -> tuple[list[str], list[dict[str, Any]]]:
    """Load and validate every pinned ``ModelLock`` and derive one contender per executable cell.

    Returns ``(contenders, contender_specs)``.  ``contenders`` is the plain string-ID list the
    signed spec declares; ``contender_specs`` additionally carries ``kind``/``channel``/
    ``model_lock_source_repository`` for ``derive_configuration_hash``.  ``pinned_models`` is
    injectable so tests can exercise this against a small synthetic fixture set instead of the
    real 9-model Gate-A inventory; it only needs a ``.logical_model_id`` attribute per entry
    (``materialize_model_locks.model_slug`` is reused verbatim for the filename mapping).
    """
    specs: list[dict[str, Any]] = []
    for pinned in pinned_models:
        logical_id = pinned.logical_model_id
        slug = model_slug(pinned)
        path = model_locks_dir / f"{slug}.json"
        if not path.is_file():
            raise BakeoffSpecBuildError(f"missing signed ModelLock for {logical_id!r}: {path}")
        lock = validate_model_lock(json.loads(path.read_text(encoding="utf-8")))
        candidate = candidate_by_model_id(logical_id)
        if candidate.kind not in {"dense", "late_interaction"}:
            raise BakeoffSpecBuildError(
                f"unsupported candidate kind {candidate.kind!r} for {logical_id}"
            )
        contender_id = f"{candidate.kind}:{logical_id}:entity"
        specs.append(
            {
                "contender_id": contender_id,
                "kind": candidate.kind,
                "channel": "entity",
                "model_lock_source_repository": lock["source_repository"],
            }
        )
    specs.append(
        {
            "contender_id": LEXICAL_CONTENDER_ID,
            "kind": "lexical",
            "channel": "bm25_plus_current_head",
            "model_lock_source_repository": None,
        }
    )
    contenders = [item["contender_id"] for item in specs]
    if len(contenders) != len(set(contenders)):
        raise BakeoffSpecBuildError("derived contender identifiers are not unique")
    return contenders, specs


def derive_configuration_hash(
    contender_specs: Sequence[Mapping[str, Any]],
    hyperparameter_grids: Mapping[str, Sequence[Any]],
) -> str:
    """Fingerprint the frozen retrieval configuration a later RetrievalRunBundle must match."""
    canonical = {
        "contenders": [
            {
                "contender_id": item["contender_id"],
                "kind": item["kind"],
                "channel": item["channel"],
                "model_lock_source_repository": item["model_lock_source_repository"],
            }
            for item in sorted(contender_specs, key=lambda item: item["contender_id"])
        ],
        "hyperparameter_grids": {
            name: list(values) for name, values in sorted(hyperparameter_grids.items())
        },
    }
    return fingerprint(canonical)


def derive_software_versions() -> dict[str, str]:
    """Read real installed package versions from this interpreter's environment."""
    versions: dict[str, str] = {"python": platform.python_version()}
    for package in _SOFTWARE_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise BakeoffSpecBuildError(
                f"required package {package!r} is not installed in this environment"
            ) from exc
    return versions


def derive_holm_comparison_family() -> list[str]:
    return list(HOLM_COMPARISON_FAMILY)


def build_bakeoff_spec(
    *,
    run_id: str,
    corpus_manifest_path: Path,
    slots_path: Path,
    assignments_path: Path,
    model_locks_dir: Path,
    repo_root: Path | None = None,
    pinned_models: Sequence[Any] = PINNED_MODELS,
    hyperparameter_grids: Mapping[str, Sequence[Any]] | None = None,
    seeds: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble, sign, and validate one Gate-D BakeoffSpec from real frozen upstream artifacts."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise BakeoffSpecBuildError(
            "run_id must be filename-safe and match bakeoff_orchestrator._SAFE_RUN_ID"
        )
    commit = commit_hash_field(repo_root)
    corpus_manifest = load_corpus_manifest(corpus_manifest_path)
    query_slots_hash = derive_query_slots_hash(slots_path, assignments_path, corpus_manifest)
    contenders, contender_specs = load_contenders(model_locks_dir, pinned_models)
    grids = dict(hyperparameter_grids) if hyperparameter_grids is not None else dict(
        HYPERPARAMETER_GRIDS
    )
    configuration_hash = derive_configuration_hash(contender_specs, grids)
    payload = {
        "run_id": run_id,
        "commit": commit,
        "corpus_snapshot_hash": corpus_manifest["corpus_root_hash"],
        "query_slots_hash": query_slots_hash,
        "query_prompt_hash": GENERATION_PROMPT_HASH,
        "rubric_hash": judge_version_fingerprint(),
        "configuration_hash": configuration_hash,
        "seeds": dict(seeds) if seeds is not None else dict(DEFAULT_SEEDS),
        "machine_fingerprint": machine_fingerprint(),
        "contenders": contenders,
        "hyperparameter_grids": grids,
        "required_metrics": list(REQUIRED_METRICS),
        "software_versions": derive_software_versions(),
        "holm_comparison_family": derive_holm_comparison_family(),
        "split_targets": dict(SPLIT_TARGETS),
        "facet_targets": FACET_TARGETS,
    }
    spec = sign_artifact("BakeoffSpec", payload)
    return validate_bakeoff_spec(spec)


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    os.replace(temporary, path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="Fresh, filename-safe run identifier")
    parser.add_argument(
        "--corpus-manifest", type=Path, required=True, help="Signed corpus_representation_manifest.json"
    )
    parser.add_argument(
        "--slots",
        type=Path,
        required=True,
        help="Private Gate-B/D slots-out sidecar JSON (build_query_slots.py --slots-out)",
    )
    parser.add_argument(
        "--assignments",
        type=Path,
        required=True,
        help="Signed QuerySlotAssignments JSON (build_query_slots.py --assignments-out)",
    )
    parser.add_argument(
        "--model-locks-dir",
        type=Path,
        required=True,
        help="Directory containing the 9 signed Gate-A ModelLock JSON files",
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--spec-out", type=Path, required=True, help="Output path for the signed BakeoffSpec")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    spec = build_bakeoff_spec(
        run_id=args.run_id,
        corpus_manifest_path=args.corpus_manifest,
        slots_path=args.slots,
        assignments_path=args.assignments,
        model_locks_dir=args.model_locks_dir,
        repo_root=args.repo_root,
    )
    _atomic_write(args.spec_out, spec)
    print(json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BakeoffContractError, BakeoffSpecBuildError) as exc:
        raise SystemExit(str(exc)) from exc
