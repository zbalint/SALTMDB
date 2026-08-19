"""Download, verify, and sign immutable local ``ModelLock`` artifacts for the pinned nine-
model Gate-A inventory (bakeoff run ``accuracy-bakeoff-20260812``).

This is the only script in this repository permitted to reach the network for model weights.
Every repository, revision, filename, and hash below was resolved by a prior session, presented
to the user as an exact 6,992,189,623-byte inventory, and explicitly approved before this file
was written -- see SALTMDB handover memory ``69536ee7-6611-4adc-ae78-448eadc331e4`` and the
Gate-A approval decision event logged at approval time. Nothing here substitutes a model,
revision, quantization, filename, or prefix policy: a mismatch between what Hugging Face serves
and the pinned expectation below is a hard failure, never a warning.

Flow per model:

1. ``snapshot_download`` the exact pinned revision into a run-private ``local_dir``, restricted
   by ``allow_patterns`` to the model file(s) plus FastEmbed's four common files.
2. Delete the ``.cache/huggingface`` bookkeeping folder that ``local_dir`` downloads create; it
   is HF-internal metadata, not part of the model's file inventory.
3. Reject any remaining symlink or non-regular file below the materialized directory.
4. Hash every regular file and compare the primary model file(s) against the pinned SHA-256.
5. Sign one ``ModelLock`` artifact via ``bakeoff_state.sign_artifact`` using the dimension,
   normalization, prefixes, and max input tokens already declared for this model in
   ``retrieval_architecture``, then validate it with ``bakeoff_state.validate_model_lock``.
6. Write the signed lock atomically to the run's ``model_locks/`` directory.

Nothing here imports FastEmbed or runs inference; that stays behind ``retrieval_adapters``, which
independently re-verifies every file's hash before any backend is constructed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_state import sign_artifact, validate_model_lock  # noqa: E402
from retrieval_architecture import candidate_by_model_id  # noqa: E402

_HASH_CHUNK_BYTES = 1024 * 1024

_COMMON_FILES: tuple[str, ...] = (
    "config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


class ModelMaterializationError(ValueError):
    """A pinned model repository, file, or hash could not be verified locally."""


@dataclass(frozen=True)
class PinnedModel:
    """One entry in the exact, user-approved Gate-A download inventory.

    ``logical_model_id`` is the id declared in ``retrieval_architecture``'s candidate table and
    is used to look up dimension/normalization/prefix metadata for the signed lock.
    ``source_repository`` is the actual Hugging Face repo the weights are fetched from, which is
    often a different ONNX-exported mirror of the logical model.
    """

    logical_model_id: str
    source_repository: str
    resolved_revision: str
    model_files: tuple[str, ...]
    expected_model_file_sha256: Mapping[str, str]
    expected_selected_total_bytes: int
    kind: str = "dense"

    def __post_init__(self) -> None:
        if set(self.model_files) != set(self.expected_model_file_sha256):
            raise ModelMaterializationError(
                f"{self.logical_model_id}: model_files must exactly match expected hash keys"
            )


PINNED_MODELS: tuple[PinnedModel, ...] = (
    PinnedModel(
        logical_model_id="BAAI/bge-small-en-v1.5",
        source_repository="Qdrant/bge-small-en-v1.5-onnx-Q",
        resolved_revision="52398278842ec682c6f32300af41344b1c0b0bb2",
        model_files=("model_optimized.onnx",),
        expected_model_file_sha256={
            "model_optimized.onnx": (
                "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"
            ),
        },
        expected_selected_total_bytes=67_179_163,
    ),
    PinnedModel(
        logical_model_id="BAAI/bge-base-en-v1.5",
        source_repository="Qdrant/bge-base-en-v1.5-onnx-Q",
        resolved_revision="738cad1c108e2f23649db9e44b2eab988626493b",
        model_files=("model_optimized.onnx",),
        expected_model_file_sha256={
            "model_optimized.onnx": (
                "4e556722bc4f65716c544c8a931f1e90fb3f866e5741fd93a96f051d673339c7"
            ),
        },
        expected_selected_total_bytes=218_538_245,
    ),
    PinnedModel(
        logical_model_id="BAAI/bge-large-en-v1.5",
        source_repository="Qdrant/bge-large-en-v1.5-onnx",
        resolved_revision="dc76b2c078fc38f0d243233d0ab0b51de925557e",
        model_files=("model.onnx",),
        expected_model_file_sha256={
            "model.onnx": "523c08e39a645aa3760380dee5d4b432b2f3ce7ba77ff2cd9c02c5a896b6a7c5",
        },
        expected_selected_total_bytes=1_337_568_357,
    ),
    PinnedModel(
        logical_model_id="snowflake/snowflake-arctic-embed-m-long",
        source_repository="Snowflake/snowflake-arctic-embed-m-long",
        resolved_revision="92d97331f1f4b6a366c1f161354b9f3390cc219f",
        model_files=("onnx/model.onnx",),
        expected_model_file_sha256={
            "onnx/model.onnx": ("76ff1f78394d5b05876aa5af3a0172ca3390fa86dc0065bf342cc3e9bb696d60"),
        },
        expected_selected_total_bytes=548_267_786,
    ),
    PinnedModel(
        logical_model_id="jinaai/jina-embeddings-v2-base-en",
        source_repository="Xenova/jina-embeddings-v2-base-en",
        resolved_revision="459a733e015d7c72b678de3611fc444a7853168a",
        model_files=("onnx/model.onnx",),
        expected_model_file_sha256={
            "onnx/model.onnx": ("f345c41919f5398da5aa3c300faed6ad58d93f96c0a2d990bc82e7012f64b6c8"),
        },
        expected_selected_total_bytes=548_075_475,
    ),
    PinnedModel(
        logical_model_id="nomic-ai/nomic-embed-text-v1.5",
        source_repository="nomic-ai/nomic-embed-text-v1.5",
        resolved_revision="e9b6763023c676ca8431644204f50c2b100d9aab",
        model_files=("onnx/model.onnx",),
        expected_model_file_sha256={
            "onnx/model.onnx": ("147d5aa88c2101237358e17796cf3a227cead1ec304ec34b465bb08e9d952965"),
        },
        expected_selected_total_bytes=548_026_095,
    ),
    PinnedModel(
        logical_model_id="mixedbread-ai/mxbai-embed-large-v1",
        source_repository="mixedbread-ai/mxbai-embed-large-v1",
        resolved_revision="b33106f585b9ce46904ad7443a3b52b7a63e231c",
        model_files=("onnx/model.onnx",),
        expected_model_file_sha256={
            "onnx/model.onnx": ("adb53ed475faa339bfad3bd2bdb7e6a30b4f47280ade9811f81bef7953f9ab77"),
        },
        expected_selected_total_bytes=1_337_568_292,
    ),
    PinnedModel(
        logical_model_id="intfloat/multilingual-e5-large",
        source_repository="Qdrant/multilingual-e5-large-onnx",
        resolved_revision="66076b8dc6e367337e3e90e6fb309fb0f3addaf6",
        model_files=("model.onnx", "model.onnx_data"),
        expected_model_file_sha256={
            "model.onnx": "1c09780c907c8a91a77a6ab1fd231f79e090d2907ca431223703dfebeed3d36c",
            "model.onnx_data": ("0cf1883fee81c63819a44e2ba0efa51d4043d9759685a4ebebbde97e0623d15c"),
        },
        expected_selected_total_bytes=2_252_994_762,
    ),
    PinnedModel(
        logical_model_id="answerdotai/answerai-colbert-small-v1",
        source_repository="answerdotai/answerai-colbert-small-v1",
        resolved_revision="c72aa89bc61afdd85373643f3a1a75b2aad6e0fe",
        model_files=("vespa_colbert.onnx",),
        expected_model_file_sha256={
            "vespa_colbert.onnx": (
                "9161e64cab96fe5a5366782578e20da3409b26bd171a2a8bc6b9168777950903"
            ),
        },
        expected_selected_total_bytes=133_971_448,
        kind="late_interaction",
    ),
)

EXPECTED_TOTAL_SELECTED_BYTES = 6_992_189_623

_total_selected_bytes = sum(model.expected_selected_total_bytes for model in PINNED_MODELS)
if _total_selected_bytes != EXPECTED_TOTAL_SELECTED_BYTES:
    raise ModelMaterializationError(
        "pinned inventory total "
        f"{_total_selected_bytes} does not match the user-approved {EXPECTED_TOTAL_SELECTED_BYTES}"
    )


def model_slug(pinned: PinnedModel) -> str:
    return pinned.logical_model_id.replace("/", "__")


def pinned_model_by_id(logical_model_id: str) -> PinnedModel:
    for pinned in PINNED_MODELS:
        if pinned.logical_model_id == logical_model_id:
            return pinned
    raise ModelMaterializationError(f"{logical_model_id} is not in the approved Gate-A inventory")


Downloader = Callable[..., "str | os.PathLike[str]"]


def _default_downloader(
    *, repo_id: str, revision: str, local_dir: Path, allow_patterns: Sequence[str]
) -> str | os.PathLike[str]:
    from huggingface_hub import snapshot_download

    return snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=local_dir,
        allow_patterns=list(allow_patterns),
    )


def download_pinned_model(
    pinned: PinnedModel,
    materialized_dir: Path,
    *,
    downloader: Downloader = _default_downloader,
) -> Path:
    """Fetch exactly the pinned revision/files for one model into ``materialized_dir``.

    ``downloader`` is injectable so tests never touch the network: it receives the same
    keyword arguments as ``huggingface_hub.snapshot_download`` and must materialize real
    regular files under ``local_dir`` (the modern ``local_dir`` download path does this by
    construction -- no symlinked blob cache).
    """
    materialized_dir.mkdir(parents=True, exist_ok=True)
    allow_patterns = tuple(pinned.model_files) + _COMMON_FILES
    downloader(
        repo_id=pinned.source_repository,
        revision=pinned.resolved_revision,
        local_dir=materialized_dir,
        allow_patterns=allow_patterns,
    )
    # ``local_dir`` downloads leave a ".cache/huggingface" bookkeeping folder at the root;
    # it is HF-internal metadata (may contain symlinks/lock files) and must never enter the
    # signed file inventory.
    bookkeeping = materialized_dir / ".cache"
    if bookkeeping.exists():
        shutil.rmtree(bookkeeping)
    return materialized_dir


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _inventory(materialized_dir: Path) -> list[tuple[str, str, int]]:
    """Hash every regular file below ``materialized_dir``; fail closed on any symlink."""
    if materialized_dir.is_symlink():
        raise ModelMaterializationError(f"{materialized_dir} must not itself be a symlink")
    if not materialized_dir.is_dir():
        raise ModelMaterializationError(f"{materialized_dir} does not exist")
    entries: list[tuple[str, str, int]] = []
    rejected: list[str] = []
    for current, directory_names, file_names in os.walk(
        materialized_dir, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            if path.is_symlink():
                rejected.append(path.relative_to(materialized_dir).as_posix())
            else:
                kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(materialized_dir).as_posix()
            if path.is_symlink() or not path.is_file():
                rejected.append(relative)
            else:
                entries.append((relative, _sha256_file(path), path.stat().st_size))
    if rejected:
        raise ModelMaterializationError(
            "materialized model cache contains symlink or non-regular entries: "
            + ", ".join(sorted(rejected))
        )
    if not entries:
        raise ModelMaterializationError(f"{materialized_dir} contains no regular files")
    return sorted(entries)


def build_model_lock(pinned: PinnedModel, materialized_dir: Path) -> dict[str, Any]:
    """Hash, cross-check, and sign a ``ModelLock`` artifact from a materialized directory."""
    entries = _inventory(materialized_dir)
    by_path = {path: (sha256, size) for path, sha256, size in entries}
    for model_file, expected_sha256 in pinned.expected_model_file_sha256.items():
        if model_file not in by_path:
            raise ModelMaterializationError(
                f"{pinned.logical_model_id}: pinned model file {model_file!r} was not materialized"
            )
        observed_sha256, _ = by_path[model_file]
        if observed_sha256 != expected_sha256:
            raise ModelMaterializationError(
                f"{pinned.logical_model_id}: {model_file} sha256 mismatch: "
                f"expected {expected_sha256}, observed {observed_sha256}"
            )
    candidate = candidate_by_model_id(pinned.logical_model_id)
    if candidate.kind != pinned.kind:
        raise ModelMaterializationError(
            f"{pinned.logical_model_id}: declared kind {candidate.kind!r} does not match "
            f"pinned kind {pinned.kind!r}"
        )
    payload = {
        "source_repository": pinned.source_repository,
        "resolved_revision": pinned.resolved_revision,
        "files": [
            {"path": path, "sha256": sha256, "size_bytes": size} for path, sha256, size in entries
        ],
        "dimension": candidate.dimension,
        "normalization": candidate.normalization,
        "maximum_input_tokens": candidate.max_input_tokens,
        "query_prefix": candidate.query_prefix,
        "document_prefix": candidate.document_prefix,
    }
    lock = sign_artifact("ModelLock", payload)
    validate_model_lock(lock)
    return lock


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def materialize_and_sign(
    pinned: PinnedModel,
    *,
    materialized_root: Path,
    lock_output_dir: Path,
    downloader: Downloader = _default_downloader,
) -> dict[str, Any]:
    """End-to-end: download, verify, sign, and persist one model's lock."""
    materialized_dir = materialized_root / model_slug(pinned)
    download_pinned_model(pinned, materialized_dir, downloader=downloader)
    lock = build_model_lock(pinned, materialized_dir)
    _atomic_write(lock_output_dir / f"{model_slug(pinned)}.json", lock)
    return lock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        default="accuracy-bakeoff-20260812",
        help="Run identifier; selects the default materialized/lock output paths under scratch/.",
    )
    parser.add_argument(
        "--materialized-root",
        type=Path,
        default=None,
        help="Override the run-private directory model caches are downloaded into.",
    )
    parser.add_argument(
        "--lock-output-dir",
        type=Path,
        default=None,
        help="Override the directory signed ModelLock JSON artifacts are written to.",
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        default=None,
        help="Logical model_id to materialize (repeatable). Defaults to all nine pinned models.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    materialized_root = args.materialized_root or repo_root / "scratch" / "models" / args.run_id
    lock_output_dir = (
        args.lock_output_dir or repo_root / "scratch" / "eval_results" / args.run_id / "model_locks"
    )
    selected = (
        tuple(pinned_model_by_id(model_id) for model_id in args.models)
        if args.models
        else PINNED_MODELS
    )

    summary = []
    for pinned in selected:
        lock = materialize_and_sign(
            pinned, materialized_root=materialized_root, lock_output_dir=lock_output_dir
        )
        summary.append(
            {
                "logical_model_id": pinned.logical_model_id,
                "source_repository": pinned.source_repository,
                "resolved_revision": pinned.resolved_revision,
                "artifact_fingerprint": lock["artifact_fingerprint"],
                "file_count": len(lock["files"]),
                "total_bytes": sum(entry["size_bytes"] for entry in lock["files"]),
            }
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
