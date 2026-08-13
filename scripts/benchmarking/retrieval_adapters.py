"""Fail-closed, local-only embedding adapters for retrieval bakeoff runs.

The architecture contracts in :mod:`retrieval_architecture` intentionally do not load models.
This module is the small executable boundary used by an eventual isolated bakeoff runner.  It
does not import FastEmbed and it never downloads anything: callers inject a backend factory and a
content-addressed :class:`ModelLock`.  The factory is called only after the local cache inventory
has been verified and always receives ``local_files_only=True``.

Dense and late-interaction backends have separate protocols on purpose.  A ColBERT model returns
variable-length token matrices and must never be accidentally routed through a fixed-width dense
vector path.  Prefix rendering is owned by the adapter, exactly once per input; backend fakes and
real FastEmbed implementations therefore receive already-rendered strings.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Literal, Mapping, Protocol, Sequence, cast

import numpy as np

from retrieval_architecture import EmbeddingSpec, LateInteractionError


Normalization = Literal["none", "l2"]
MaxSimReduction = Literal["sum", "mean"]

_SHA256_LENGTH = 64
_L2_TOLERANCE = 1e-4
_HASH_CHUNK_BYTES = 1024 * 1024


class AdapterError(ValueError):
    """Base error for invalid adapter inputs or backend output."""


class ModelLockError(AdapterError):
    """Raised when the requested model lock cannot be verified locally."""


class BackendContractError(AdapterError):
    """Raised when an injected backend violates its adapter protocol."""


class PrefixContractError(AdapterError):
    """Raised when an input is already prefixed and would otherwise be double-rendered."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One immutable file entry in a model-cache inventory."""

    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        normalized = _validate_relative_inventory_path(self.path)
        object.__setattr__(self, "path", normalized)
        if not isinstance(self.sha256, str) or len(self.sha256) != _SHA256_LENGTH:
            raise ModelLockError(f"model file {self.path!r} sha256 must be 64 hexadecimal characters")
        if any(character not in "0123456789abcdef" for character in self.sha256):
            raise ModelLockError(f"model file {self.path!r} sha256 must be lowercase hexadecimal")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise ModelLockError(f"model file {self.path!r} size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ModelLockError(f"model file {self.path!r} size_bytes must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ModelLock:
    """A model specification bound to an exact local file inventory.

    ``spec`` is the same immutable representation used by the benchmark contracts.  ``cache_path``
    identifies the local model directory, while ``files`` names *every* file allowed below it.
    Verification rejects missing, unexpected, symlinked, size-mismatched, or hash-mismatched files.
    ``from_directory`` is convenient for creating a lock in a controlled preparation step; a
    bakeoff run should persist and review the resulting inventory before using it.
    """

    spec: EmbeddingSpec
    cache_path: Path
    files: tuple[ModelFile, ...]
    fastembed_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, EmbeddingSpec):
            raise ModelLockError("model lock spec must be an EmbeddingSpec")
        cache_path = Path(self.cache_path)
        if not cache_path.is_absolute():
            cache_path = cache_path.absolute()
        object.__setattr__(self, "cache_path", cache_path)

        entries: list[ModelFile] = []
        for entry in self.files:
            if isinstance(entry, ModelFile):
                entries.append(entry)
            elif isinstance(entry, Mapping):
                try:
                    entries.append(
                        ModelFile(
                            path=cast(str, entry["path"]),
                            sha256=cast(str, entry["sha256"]),
                            size_bytes=cast(int, entry["size_bytes"]),
                        )
                    )
                except KeyError as exc:
                    raise ModelLockError(f"model file inventory is missing {exc.args[0]!r}") from exc
            else:
                raise ModelLockError("model file inventory entries must be ModelFile or mappings")
        if not entries:
            raise ModelLockError("model file inventory must be non-empty")
        paths = [entry.path for entry in entries]
        if len(paths) != len(set(paths)):
            raise ModelLockError("model file inventory paths must be unique")
        object.__setattr__(self, "files", tuple(sorted(entries, key=lambda entry: entry.path)))
        if self.fastembed_version is not None and (
            not isinstance(self.fastembed_version, str) or not self.fastembed_version
        ):
            raise ModelLockError("fastembed_version must be a non-empty string when supplied")

    @property
    def inventory(self) -> tuple[ModelFile, ...]:
        """Alias used by callers that call the entries an inventory."""
        return self.files

    @property
    def cache_dir(self) -> Path:
        """Alias matching FastEmbed's constructor terminology."""
        return self.cache_path

    @property
    def model_id(self) -> str:
        return self.spec.model_id

    @property
    def revision(self) -> str | None:
        return self.spec.revision

    @property
    def dimension(self) -> int:
        return self.spec.dimension

    @classmethod
    def from_directory(
        cls,
        spec: EmbeddingSpec,
        cache_path: str | os.PathLike[str],
        *,
        fastembed_version: str | None = None,
    ) -> "ModelLock":
        """Create an inventory from a local directory without importing a model runtime."""
        root = _require_cache_directory(Path(cache_path))
        files = tuple(
            ModelFile(
                path=relative,
                sha256=_sha256_file(root / relative),
                size_bytes=(root / relative).stat().st_size,
            )
            for relative in _actual_file_paths(root)
        )
        if not files:
            raise ModelLockError(f"model cache directory {root} contains no regular files")
        return cls(spec, root, files, fastembed_version=fastembed_version)

    def to_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_dict(),
            "cache_path": str(self.cache_path),
            "files": [entry.to_dict() for entry in self.files],
            "fastembed_version": self.fastembed_version,
        }


@dataclass(frozen=True, slots=True)
class VerifiedInventory:
    """Evidence returned after a model lock has passed local verification."""

    cache_path: Path
    files: tuple[ModelFile, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)


def _validate_relative_inventory_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ModelLockError("model file path must be a non-empty relative string")
    if "\\" in value:
        raise ModelLockError(f"model file path {value!r} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ModelLockError(f"model file path {value!r} must be normalized and relative")
    normalized = path.as_posix()
    if normalized != value or normalized == ".":
        raise ModelLockError(f"model file path {value!r} must be normalized and relative")
    return normalized


def _require_cache_directory(root: Path) -> Path:
    if root.is_symlink():
        raise ModelLockError(f"model cache path {root} must not be a symlink")
    if not root.exists():
        raise ModelLockError(f"model cache path {root} does not exist")
    if not root.is_dir():
        raise ModelLockError(f"model cache path {root} is not a directory")
    return root


def _actual_file_paths(root: Path) -> tuple[str, ...]:
    """Return all regular-file paths and fail closed on symlinks/reparse-like entries."""
    root = _require_cache_directory(root)
    paths: list[str] = []
    symlinks: list[str] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for directory_name in directory_names:
            path = current_path / directory_name
            if path.is_symlink():
                symlinks.append(path.relative_to(root).as_posix())
            else:
                kept_directories.append(directory_name)
        directory_names[:] = kept_directories
        for file_name in file_names:
            path = current_path / file_name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                symlinks.append(relative)
            else:
                paths.append(relative)
    if symlinks:
        names = ", ".join(sorted(symlinks))
        raise ModelLockError(f"model cache contains symlink or non-regular entries: {names}")
    return tuple(sorted(paths))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(_HASH_CHUNK_BYTES):
                digest.update(block)
    except OSError as exc:
        raise ModelLockError(f"cannot hash model file {path}: {exc}") from exc
    return digest.hexdigest()


def verify_model_lock(lock: ModelLock, cache_path: str | os.PathLike[str] | None = None) -> VerifiedInventory:
    """Verify every file below ``cache_path`` against ``lock`` and return checked evidence.

    The comparison is exact: no expected file may be missing and no local file may be unexpected.
    Hash and byte-size checks are both performed for existing expected files.  All discovered
    mismatches are reported together so an operator can repair one immutable cache deliberately.
    """
    if not isinstance(lock, ModelLock):
        raise ModelLockError("lock must be a ModelLock")
    root = _require_cache_directory(Path(lock.cache_path if cache_path is None else cache_path))
    expected = {entry.path: entry for entry in lock.files}
    actual = set(_actual_file_paths(root))
    expected_paths = set(expected)
    errors: list[str] = []
    missing = sorted(expected_paths - actual)
    unexpected = sorted(actual - expected_paths)
    if missing:
        errors.append("missing files: " + ", ".join(missing))
    if unexpected:
        errors.append("unexpected files: " + ", ".join(unexpected))

    for relative in sorted(expected_paths & actual):
        path = root / relative
        entry = expected[relative]
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"size check failed for {relative}: {exc}")
            continue
        if size != entry.size_bytes:
            errors.append(
                f"size mismatch for {relative}: expected {entry.size_bytes}, observed {size}"
            )
        try:
            digest = _sha256_file(path)
        except ModelLockError as exc:
            errors.append(str(exc))
            continue
        if digest != entry.sha256:
            errors.append(
                f"hash mismatch for {relative}: expected {entry.sha256}, observed {digest}"
            )
    if errors:
        raise ModelLockError("; ".join(errors))
    return VerifiedInventory(root, tuple(lock.files))


# A descriptive alias for callers that prefer the longer name in benchmark code.
verify_model_lock_inventory = verify_model_lock


def _require_spec_lock_match(spec: EmbeddingSpec, lock: ModelLock) -> None:
    if not isinstance(spec, EmbeddingSpec):
        raise AdapterError("adapter spec must be an EmbeddingSpec")
    if not isinstance(lock, ModelLock):
        raise ModelLockError("adapter lock must be a ModelLock")
    if spec.identity_key != lock.spec.identity_key:
        raise ModelLockError("model lock spec does not exactly match adapter embedding spec")


def _render_once(value: str, prefix: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise AdapterError(f"{field_name} text must be a string")
    if prefix and value.startswith(prefix):
        raise PrefixContractError(
            f"{field_name} text already starts with the configured prefix; refusing double rendering"
        )
    return prefix + value


def _as_matrix(raw: object, *, field_name: str, expected_count: int | None = None) -> list[np.ndarray]:
    """Coerce one backend batch into independent numpy arrays without accepting scalars."""
    if isinstance(raw, np.ndarray):
        if raw.ndim == 1:
            rows: list[object] = [raw]
        elif raw.ndim >= 2:
            rows = [raw[index] for index in range(raw.shape[0])]
        else:
            raise BackendContractError(f"{field_name} backend output has no dimensions")
    else:
        if isinstance(raw, (str, bytes)):
            raise BackendContractError(f"{field_name} backend output must be an array sequence")
        try:
            rows = list(cast(Iterable[object], raw))
        except TypeError as exc:
            raise BackendContractError(f"{field_name} backend output must be iterable") from exc
    if expected_count is not None and len(rows) != expected_count:
        raise BackendContractError(
            f"{field_name} backend returned {len(rows)} rows for {expected_count} inputs"
        )
    try:
        return [np.asarray(row, dtype=np.float32) for row in rows]
    except (TypeError, ValueError) as exc:
        raise BackendContractError(f"{field_name} backend output is not numeric") from exc


def _as_single(raw: object, *, field_name: str) -> np.ndarray:
    if isinstance(raw, np.ndarray) and raw.ndim == 1:
        return np.asarray(raw, dtype=np.float32)
    rows = _as_matrix(raw, field_name=field_name)
    if len(rows) != 1:
        raise BackendContractError(f"{field_name} backend must return exactly one vector/matrix")
    return rows[0]


def _as_single_token_matrix(raw: object, *, field_name: str) -> np.ndarray:
    """Coerce FastEmbed's one-query token output without mistaking tokens for documents."""
    if isinstance(raw, np.ndarray) and raw.ndim == 2:
        return np.asarray(raw, dtype=np.float32)
    if isinstance(raw, (str, bytes)):
        raise BackendContractError(f"{field_name} backend output must be a token matrix")
    try:
        values = list(cast(Iterable[object], raw))
    except TypeError as exc:
        raise BackendContractError(f"{field_name} backend output must be iterable") from exc
    if len(values) == 1:
        try:
            candidate = np.asarray(values[0], dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise BackendContractError(f"{field_name} backend output is not numeric") from exc
        if candidate.ndim == 2:
            return candidate
    if values:
        try:
            candidate = np.asarray(values, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise BackendContractError(f"{field_name} backend output is not numeric") from exc
        if candidate.ndim == 2:
            return candidate
    raise BackendContractError(f"{field_name} backend must return exactly one token matrix")


def _l2_normalize_dense_vector(vector: np.ndarray, *, field_name: str) -> np.ndarray:
    """Rescale a raw backend vector to unit L2 norm before the strict validator sees it.

    Some FastEmbed model classes (confirmed empirically: ``nomic-ai/nomic-embed-text-v1.5`` is
    registered under FastEmbed's ``PooledEmbedding`` class, which only mean-pools and does not
    L2-normalize) do not themselves produce unit-norm output even when the model is conventionally
    used with cosine similarity and this project's ``EmbeddingSpec.normalization`` declares
    ``"l2"``.  ``retrieval_index_runner.py`` scores with a raw dot product, so an unnormalized
    vector is not merely a magnitude inconsistency versus other contenders -- it changes the
    ranking math itself, since dot-product-as-cosine-substitute is only correct on true unit
    vectors.  This adapter is therefore responsible for *guaranteeing* the declared contract holds
    for any backend, not merely observing whether it happens to; ``_validate_dense_vector`` below
    remains a strict, unmodified post-normalization sanity check (defense in depth against
    non-finite/degenerate output), not the primary correctness mechanism.  Already-normalized
    backends (BGE, Snowflake Arctic) are unaffected: dividing an already-unit vector by its own
    (already ~1.0) norm is a numerically safe no-op, not a behavior change.
    """
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 0.0:
        raise BackendContractError(
            f"{field_name} cannot be l2-normalized: non-finite or non-positive norm {norm:.8g}"
        )
    return vector / norm


def _validate_dense_vector(vector: np.ndarray, spec: EmbeddingSpec, *, field_name: str) -> np.ndarray:
    if vector.ndim != 1 or vector.shape[0] != spec.dimension:
        raise BackendContractError(
            f"{field_name} dimension mismatch: expected ({spec.dimension},), observed {vector.shape}"
        )
    if not np.isfinite(vector).all():
        raise BackendContractError(f"{field_name} contains non-finite values")
    if spec.normalization == "l2":
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm <= 0.0 or not np.isclose(
            norm, 1.0, rtol=_L2_TOLERANCE, atol=_L2_TOLERANCE
        ):
            raise BackendContractError(
                f"{field_name} violates l2 normalization: observed norm {norm:.8g}"
            )
    return np.array(vector, dtype=np.float32, copy=True)


def _validate_late_matrix(matrix: np.ndarray, spec: EmbeddingSpec, *, field_name: str) -> np.ndarray:
    if matrix.ndim != 2 or matrix.shape[1] != spec.dimension or matrix.shape[0] <= 0:
        raise BackendContractError(
            f"{field_name} token matrix must have shape (tokens, {spec.dimension}), observed {matrix.shape}"
        )
    if not np.isfinite(matrix).all():
        raise BackendContractError(f"{field_name} contains non-finite values")
    if spec.normalization == "l2":
        norms = np.linalg.norm(matrix, axis=1)
        if not np.isfinite(norms).all() or (norms <= 0.0).any() or not np.allclose(
            norms, 1.0, rtol=_L2_TOLERANCE, atol=_L2_TOLERANCE
        ):
            raise BackendContractError(f"{field_name} violates per-token l2 normalization")
    return np.array(matrix, dtype=np.float32, copy=True)


class DenseBackend(Protocol):
    """Minimal dense backend surface implemented by FastEmbed TextEmbedding or a fake."""

    def embed(self, documents: Sequence[str]) -> Iterable[object]: ...

    def query_embed(self, query: str) -> object: ...


class LateInteractionBackend(Protocol):
    """Minimal token backend surface implemented by FastEmbed LateInteractionTextEmbedding."""

    def passage_embed(self, documents: Sequence[str]) -> Iterable[object]: ...

    def query_embed(self, query: str) -> object: ...


BackendFactory = Callable[..., object]


def fastembed_dense_factory(
    *,
    model_name: str,
    cache_dir: str,
    local_files_only: bool,
    specific_model_path: str | None = None,
) -> object:
    """Lazy real FastEmbed factory; callers must still supply a verified local ModelLock.

    ``specific_model_path`` bypasses FastEmbed's hub-cache-layout file discovery entirely: when
    set, ``fastembed.common.model_management.download_model`` returns that path directly instead
    of guessing a hub-style directory layout under ``cache_dir``.  Gate A's flat
    ``snapshot_download(..., local_dir=...)`` download does not produce that hub layout, so this
    is the correct bypass, not merely an optional hint.
    """
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - dependency is declared by the project
        raise BackendContractError("FastEmbed TextEmbedding is unavailable") from exc
    return TextEmbedding(
        model_name=model_name,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
        specific_model_path=specific_model_path,
    )


def fastembed_late_interaction_factory(
    *,
    model_name: str,
    cache_dir: str,
    local_files_only: bool,
    specific_model_path: str | None = None,
) -> object:
    """Lazy real ColBERT factory with the same offline-only adapter boundary.

    See :func:`fastembed_dense_factory` for why ``specific_model_path`` is required rather than
    optional in practice: it is the only way to point FastEmbed at a flat, non-hub-layout cache.
    """
    try:
        from fastembed import LateInteractionTextEmbedding
    except ImportError as exc:  # pragma: no cover - dependency is declared by the project
        raise BackendContractError("FastEmbed LateInteractionTextEmbedding is unavailable") from exc
    return LateInteractionTextEmbedding(
        model_name=model_name,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
        specific_model_path=specific_model_path,
    )


def _create_local_backend(
    factory: BackendFactory,
    *,
    model_name: str,
    cache_path: Path,
) -> object:
    """Instantiate an injected backend with the non-negotiable offline controls.

    ``specific_model_path`` is passed alongside ``cache_dir`` so FastEmbed's real factories can
    bypass hub-cache-layout file discovery entirely (see :func:`fastembed_dense_factory`); passing
    both is harmless for fakes/tests and gives FastEmbed a sane fallback location if
    ``specific_model_path`` were ever unused by some other injected factory.
    """
    try:
        backend = factory(
            model_name=model_name,
            cache_dir=str(cache_path),
            local_files_only=True,
            specific_model_path=str(cache_path),
        )
    except Exception as exc:
        raise BackendContractError(
            "local-only backend factory failed; no online fallback is permitted"
        ) from exc
    if backend is None:
        raise BackendContractError("local-only backend factory returned None")
    return backend


class DenseEmbeddingAdapter:
    """Execute dense query/document embeddings against a verified local model cache."""

    def __init__(
        self,
        spec: EmbeddingSpec,
        model_lock: ModelLock,
        backend_factory: BackendFactory,
    ) -> None:
        if not callable(backend_factory):
            raise AdapterError("backend_factory must be callable")
        if spec.kind != "dense" or "colbert" in spec.model_id.lower():
            raise LateInteractionError(
                f"{spec.model_id} is a late-interaction/ColBERT model; dense adapter is forbidden"
            )
        spec.require_dense()
        _require_spec_lock_match(spec, model_lock)
        verify_model_lock(model_lock)
        self.spec = spec
        self.model_lock = model_lock
        backend = _create_local_backend(
            backend_factory,
            model_name=spec.model_id,
            cache_path=model_lock.cache_path,
        )
        if not hasattr(backend, "embed") or not callable(getattr(backend, "embed")):
            raise BackendContractError("dense backend must expose callable embed(documents)")
        if not hasattr(backend, "query_embed") or not callable(getattr(backend, "query_embed")):
            raise BackendContractError("dense backend must expose callable query_embed(query)")
        self._backend = cast(DenseBackend, backend)

    @property
    def dimension(self) -> int:
        return self.spec.dimension

    @property
    def compatibility_key(self) -> str:
        return self.spec.compatibility_key()

    def render_query(self, query: str) -> str:
        return _render_once(query, self.spec.query_prefix, "query")

    def render_document(self, document: str) -> str:
        return _render_once(document, self.spec.document_prefix, "document")

    def embed_documents(self, documents: Sequence[str]) -> np.ndarray:
        if isinstance(documents, (str, bytes)):
            raise AdapterError("documents must be a sequence of strings, not one string")
        values = list(documents)
        rendered = [self.render_document(document) for document in values]
        if not rendered:
            return np.empty((0, self.spec.dimension), dtype=np.float32)
        try:
            raw = self._backend.embed(rendered)
        except Exception as exc:
            raise BackendContractError("dense document embedding failed") from exc
        rows = _as_matrix(raw, field_name="document", expected_count=len(rendered))
        if self.spec.normalization == "l2":
            rows = np.stack(
                [
                    _l2_normalize_dense_vector(row, field_name=f"document[{index}]")
                    for index, row in enumerate(rows)
                ],
                axis=0,
            )
        validated = [
            _validate_dense_vector(row, self.spec, field_name=f"document[{index}]")
            for index, row in enumerate(rows)
        ]
        return np.stack(validated, axis=0)

    def embed_document(self, document: str) -> np.ndarray:
        return self.embed_documents([document])[0]

    def embed_query(self, query: str) -> np.ndarray:
        rendered = self.render_query(query)
        try:
            raw = self._backend.query_embed(rendered)
        except Exception as exc:
            raise BackendContractError("dense query embedding failed") from exc
        vector = _as_single(raw, field_name="query")
        if self.spec.normalization == "l2":
            vector = _l2_normalize_dense_vector(vector, field_name="query")
        return _validate_dense_vector(vector, self.spec, field_name="query")

    # FastEmbed terminology aliases make the adapter usable by a small runner without exposing
    # the backend object itself.
    embed = embed_documents
    query_embed = embed_query


def maxsim(
    query_tokens: object,
    document_tokens: object,
    *,
    reduction: MaxSimReduction = "sum",
) -> float:
    """Compute pure ColBERT MaxSim: max document-token similarity per query token, then reduce."""
    if reduction not in {"sum", "mean"}:
        raise AdapterError("MaxSim reduction must be 'sum' or 'mean'")
    query = np.asarray(query_tokens, dtype=np.float32)
    document = np.asarray(document_tokens, dtype=np.float32)
    if query.ndim != 2 or document.ndim != 2 or query.shape[1] != document.shape[1]:
        raise BackendContractError(
            "MaxSim inputs must be two-dimensional token matrices with matching dimensions"
        )
    if query.shape[0] <= 0 or document.shape[0] <= 0:
        raise BackendContractError("MaxSim inputs must contain at least one token")
    if not np.isfinite(query).all() or not np.isfinite(document).all():
        raise BackendContractError("MaxSim inputs contain non-finite values")
    similarities = np.asarray(query, dtype=np.float64) @ np.asarray(document, dtype=np.float64).T
    maxima = np.max(similarities, axis=1)
    score = float(np.sum(maxima) if reduction == "sum" else np.mean(maxima))
    if not np.isfinite(score):
        raise BackendContractError("MaxSim produced a non-finite score")
    return score


# Spelling used by some retrieval literature and callers.
max_sim = maxsim


class LateInteractionEmbeddingAdapter:
    """Execute local-only token embeddings and MaxSim for a late-interaction model."""

    def __init__(
        self,
        spec: EmbeddingSpec,
        model_lock: ModelLock,
        backend_factory: BackendFactory,
        *,
        reduction: MaxSimReduction = "sum",
    ) -> None:
        if not callable(backend_factory):
            raise AdapterError("backend_factory must be callable")
        if spec.kind != "late_interaction":
            raise LateInteractionError(
                f"{spec.model_id} is {spec.kind}; late-interaction adapter requires late_interaction"
            )
        if reduction not in {"sum", "mean"}:
            raise AdapterError("MaxSim reduction must be 'sum' or 'mean'")
        _require_spec_lock_match(spec, model_lock)
        verify_model_lock(model_lock)
        self.spec = spec
        self.model_lock = model_lock
        self.reduction = reduction
        backend = _create_local_backend(
            backend_factory,
            model_name=spec.model_id,
            cache_path=model_lock.cache_path,
        )
        if not hasattr(backend, "passage_embed") or not callable(
            getattr(backend, "passage_embed")
        ):
            raise BackendContractError(
                "late-interaction backend must expose callable passage_embed(documents)"
            )
        if not hasattr(backend, "query_embed") or not callable(getattr(backend, "query_embed")):
            raise BackendContractError("late-interaction backend must expose callable query_embed(query)")
        self._backend = cast(LateInteractionBackend, backend)

    @property
    def dimension(self) -> int:
        return self.spec.dimension

    @property
    def compatibility_key(self) -> str:
        return self.spec.compatibility_key()

    def render_query(self, query: str) -> str:
        return _render_once(query, self.spec.query_prefix, "query")

    def render_document(self, document: str) -> str:
        return _render_once(document, self.spec.document_prefix, "document")

    def embed_documents(self, documents: Sequence[str]) -> tuple[np.ndarray, ...]:
        if isinstance(documents, (str, bytes)):
            raise AdapterError("documents must be a sequence of strings, not one string")
        values = list(documents)
        rendered = [self.render_document(document) for document in values]
        if not rendered:
            return ()
        try:
            raw = self._backend.passage_embed(rendered)
        except Exception as exc:
            raise BackendContractError("late-interaction document embedding failed") from exc
        rows = _as_matrix(raw, field_name="document", expected_count=len(rendered))
        return tuple(
            _validate_late_matrix(row, self.spec, field_name=f"document[{index}]")
            for index, row in enumerate(rows)
        )

    def embed_document(self, document: str) -> np.ndarray:
        return self.embed_documents([document])[0]

    def embed_query(self, query: str) -> np.ndarray:
        rendered = self.render_query(query)
        try:
            raw = self._backend.query_embed(rendered)
        except Exception as exc:
            raise BackendContractError("late-interaction query embedding failed") from exc
        matrix = _as_single_token_matrix(raw, field_name="query")
        return _validate_late_matrix(matrix, self.spec, field_name="query")

    def maxsim(self, query_tokens: object, document_tokens: object) -> float:
        return maxsim(query_tokens, document_tokens, reduction=self.reduction)

    def score(self, query: str, document: str) -> float:
        return maxsim(self.embed_query(query), self.embed_document(document), reduction=self.reduction)

    embed = embed_documents
    query_embed = embed_query


# More concise alias for callers that use "ColBERT" as the channel name.
ColBERTAdapter = LateInteractionEmbeddingAdapter
DenseAdapter = DenseEmbeddingAdapter
LateInteractionAdapter = LateInteractionEmbeddingAdapter
DenseFastEmbedAdapter = DenseEmbeddingAdapter
ColbertFastEmbedAdapter = LateInteractionEmbeddingAdapter


__all__ = [
    "AdapterError",
    "BackendContractError",
    "ColBERTAdapter",
    "ColbertFastEmbedAdapter",
    "DenseBackend",
    "DenseAdapter",
    "DenseFastEmbedAdapter",
    "DenseEmbeddingAdapter",
    "LateInteractionBackend",
    "LateInteractionAdapter",
    "LateInteractionEmbeddingAdapter",
    "ModelFile",
    "ModelLock",
    "ModelLockError",
    "PrefixContractError",
    "VerifiedInventory",
    "max_sim",
    "maxsim",
    "verify_model_lock",
    "verify_model_lock_inventory",
]
