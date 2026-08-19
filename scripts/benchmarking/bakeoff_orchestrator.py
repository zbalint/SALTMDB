"""No-download orchestration for one signed search-accuracy bakeoff run.

The retrieval, model, and judge workers remain outside this module.  This layer only reads
signed control/evidence artifacts, advances the state machine, and keeps immutable receipt files
for every transition.  In particular, it has no operation that opens a blind query manifest:
blind query material belongs in :class:`bakeoff_state.BlindVault` and is intentionally outside
the CLI surface here.

Typical usage::

    python scripts/benchmarking/bakeoff_orchestrator.py init \
        --run-dir RUN --spec bakeoff_spec.json
    python scripts/benchmarking/bakeoff_orchestrator.py advance \
        --run-dir RUN --spec bakeoff_spec.json --target DEV_INDEXED \
        --evidence index_receipt.json

The command prints only state/fingerprint metadata.  It never prints artifact bodies.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # POSIX is the supported bakeoff runner; keep importable on non-POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is single-process only.
    fcntl = None  # type: ignore[assignment]

try:  # Sibling-script execution and direct import from the test suite.
    from bakeoff_state import (
        ALLOWED_TRANSITIONS,
        TRANSITION_EVIDENCE_KINDS,
        BakeoffContractError,
        BakeoffStateMachine,
        RunState,
        sign_artifact,
        validate_bakeoff_spec,
        validate_blind_evaluation,
        validate_blind_manifest_receipt,
        validate_blind_unlock,
        validate_corpus_manifest,
        validate_development_winner,
        validate_model_lock,
        validate_promotion_decision,
        validate_query_slot_assignments,
        validate_signed_artifact,
    )
except ModuleNotFoundError:  # pragma: no cover - package-style fallback
    from scripts.benchmarking.bakeoff_state import (
        ALLOWED_TRANSITIONS,
        TRANSITION_EVIDENCE_KINDS,
        BakeoffContractError,
        BakeoffStateMachine,
        RunState,
        sign_artifact,
        validate_bakeoff_spec,
        validate_blind_evaluation,
        validate_blind_manifest_receipt,
        validate_blind_unlock,
        validate_corpus_manifest,
        validate_development_winner,
        validate_model_lock,
        validate_promotion_decision,
        validate_query_slot_assignments,
        validate_signed_artifact,
    )


RECEIPT_SCHEMA_VERSION = 1
RECEIPT_DIRNAME = "receipts"
RECEIPT_INDEX_NAME = "receipt_index.json"
CHECKPOINT_HISTORY_DIRNAME = "checkpoints"
CHECKPOINT_INDEX_NAME = "checkpoint_index.json"
LOCK_NAME = ".orchestrator.lock"
SPEC_COPY_NAME = "bakeoff_spec.json"
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BLIND_QUERY_NAME = re.compile(
    r"(?i)(?:^|[_-])(?:queries?|targets?|slots?)[_-]?blind(?:[_.-]|$)"
    r"|blind[_-]?(?:queries?|targets?|slots?)(?:[_.-]|$)"
)


class OrchestrationError(BakeoffContractError):
    """Raised when an orchestration input or immutable receipt chain is invalid."""


def _atomic_write_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Write a JSON object with fsync + replace, never exposing a partial artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_control_artifact(path: Path) -> dict[str, Any]:
    """Read one control/evidence artifact after rejecting blind-query-looking paths.

    This filename check is intentionally before ``read_text``.  The orchestrator accepts only
    signed control artifacts; rejecting the conventional blind-query names up front makes an
    accidental ``--evidence queries_blind.json`` invocation fail without touching that file.
    """
    path = Path(path)
    if _BLIND_QUERY_NAME.search(path.name):
        raise OrchestrationError("blind query manifests are not orchestration evidence")
    if path.suffix.casefold() != ".json":
        raise OrchestrationError("orchestration artifacts must be JSON files")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"cannot read signed artifact {path}") from exc
    if not isinstance(value, dict):
        raise OrchestrationError("signed artifact must be a JSON object")
    return value


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    """Create an immutable receipt once; refuse replacement with different content."""
    path = Path(path)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestrationError(f"receipt path is unreadable: {path}") from exc
        if existing != dict(value):
            raise OrchestrationError(f"immutable receipt collision: {path.name}")
        return
    _atomic_write_json(path, value)


def _state(value: RunState | str) -> RunState:
    try:
        return value if isinstance(value, RunState) else RunState(value)
    except (TypeError, ValueError) as exc:
        raise OrchestrationError(f"unknown bakeoff state: {value!r}") from exc


def _artifact_path(run_dir: Path, name: str) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise OrchestrationError("evidence name must be non-empty")
    if Path(name).name != name or name in {".", ".."}:
        raise OrchestrationError("evidence names must be simple filenames")
    return Path(run_dir) / RECEIPT_DIRNAME / name


def _validate_transition_artifact(  # noqa: C901
    target: RunState,
    artifact: Mapping[str, Any],
    spec: Mapping[str, Any],
    winner: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Run the exact typed validator required by the target state."""
    try:
        expected_kind = TRANSITION_EVIDENCE_KINDS.get(target)
        if expected_kind is None:
            raise OrchestrationError(f"state {target.value} does not accept evidence")
        if artifact.get("kind") != expected_kind:
            raise OrchestrationError(
                f"transition to {target.value} requires {expected_kind} evidence"
            )
        if target is RunState.DEV_WINNER_SIGNED:
            return validate_development_winner(artifact, spec)
        if target is RunState.BLIND_UNLOCKED:
            if winner is None:
                raise OrchestrationError(
                    "BlindUnlock validation requires the signed development winner"
                )
            return validate_blind_unlock(artifact, spec, winner)
        if target in {RunState.PROMOTED, RunState.RETAINED}:
            return validate_promotion_decision(artifact, spec)
        validated = validate_signed_artifact(artifact, kind=expected_kind)
        # Every stage receipt must carry both bindings.  Without these fields a validly signed
        # artifact from another run would be indistinguishable from current evidence.
        if validated.get("run_id") != spec["run_id"]:
            raise OrchestrationError("evidence belongs to a different bakeoff run")
        if validated.get("spec_fingerprint") != spec["artifact_fingerprint"]:
            raise OrchestrationError("evidence is bound to a different BakeoffSpec")
        # These validators are not transition kinds themselves, but producers may attach the
        # frozen contracts to a stage receipt.  Validate them at this boundary rather than
        # accepting an unchecked nested manifest/lock.
        nested_validators = {
            "model_lock": validate_model_lock,
            "corpus_manifest": validate_corpus_manifest,
            "query_slot_assignments": validate_query_slot_assignments,
        }
        for field, validator in nested_validators.items():
            if field in validated:
                validator(validated[field])
        return validated
    except OrchestrationError:
        raise
    except (BakeoffContractError, PermissionError) as exc:
        raise OrchestrationError(str(exc)) from exc


class BakeoffOrchestrator:
    """Initialize/advance one bakeoff while retaining a verifiable receipt chain."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        if self.run_dir.name and not _SAFE_RUN_ID.fullmatch(self.run_dir.name):
            raise OrchestrationError("run directory name is not filename-safe")
        self.machine = BakeoffStateMachine(self.run_dir)
        self.receipts_dir = self.run_dir / RECEIPT_DIRNAME
        self.index_path = self.receipts_dir / RECEIPT_INDEX_NAME
        self.checkpoints_dir = self.run_dir / CHECKPOINT_HISTORY_DIRNAME
        self.checkpoint_index_path = self.checkpoints_dir / CHECKPOINT_INDEX_NAME
        self.lock_path = self.run_dir / LOCK_NAME

    @property
    def spec_path(self) -> Path:
        return self.run_dir / SPEC_COPY_NAME

    def _load_spec(self, spec_path: Path | None = None) -> dict[str, Any]:
        path = Path(spec_path) if spec_path is not None else self.spec_path
        if _BLIND_QUERY_NAME.search(path.name):
            raise OrchestrationError("BakeoffSpec cannot be a blind query manifest")
        value = _read_control_artifact(path)
        return validate_bakeoff_spec(value)

    def _load_receipt_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": None,
                "receipts": [],
            }
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestrationError("receipt index is unreadable") from exc
        if not isinstance(value, dict):
            raise OrchestrationError("receipt index must be an object")
        try:
            validate_signed_artifact(value, kind="ReceiptIndex")
        except BakeoffContractError as exc:
            raise OrchestrationError("receipt index signature is invalid") from exc
        if value.get("index_schema_version") != RECEIPT_SCHEMA_VERSION:
            raise OrchestrationError("unsupported receipt index schema")
        receipts = value.get("receipts")
        if not isinstance(receipts, list) or not all(isinstance(item, str) for item in receipts):
            raise OrchestrationError("receipt index has malformed receipt names")
        return value

    def _load_checkpoint_index(self) -> dict[str, Any]:
        if not self.checkpoint_index_path.exists():
            return {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": None,
                "checkpoints": [],
            }
        try:
            value = json.loads(self.checkpoint_index_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise OrchestrationError("checkpoint index must be an object")
            validate_signed_artifact(value, kind="CheckpointHistoryIndex")
        except OrchestrationError:
            raise
        except (OSError, json.JSONDecodeError, BakeoffContractError) as exc:
            raise OrchestrationError("checkpoint index signature is invalid") from exc
        if value.get("index_schema_version") != RECEIPT_SCHEMA_VERSION:
            raise OrchestrationError("unsupported checkpoint index schema")
        checkpoints = value.get("checkpoints")
        if not isinstance(checkpoints, list) or not all(
            isinstance(item, str) for item in checkpoints
        ):
            raise OrchestrationError("checkpoint index has malformed checkpoint names")
        return value

    @contextmanager
    def _run_lock(self) -> Iterator[None]:
        """Serialize cooperating orchestrators so checkpoint replacement is a CAS operation."""
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.run_dir, 0o700)
        try:
            handle = self.lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise OrchestrationError("cannot open the orchestration lock") from exc
        with handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _record_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        """Persist an immutable checkpoint copy before its current file can be superseded."""
        try:
            validate_signed_artifact(checkpoint, kind="BakeoffCheckpoint")
        except BakeoffContractError as exc:
            raise OrchestrationError("state machine returned an invalid checkpoint") from exc
        index = self._load_checkpoint_index()
        run_id = checkpoint.get("run_id")
        if index["run_id"] not in (None, run_id):
            raise OrchestrationError("checkpoint history belongs to another run")
        previous: str | None = None
        if index["checkpoints"]:
            previous_name = index["checkpoints"][-1]
            previous_path = self.checkpoints_dir / previous_name
            try:
                previous_value = json.loads(previous_path.read_text(encoding="utf-8"))
                validate_signed_artifact(previous_value, kind="BakeoffCheckpoint")
            except (OSError, json.JSONDecodeError, BakeoffContractError) as exc:
                raise OrchestrationError("checkpoint history predecessor is invalid") from exc
            previous = previous_value["artifact_fingerprint"]
            if checkpoint.get("previous_checkpoint_fingerprint") != previous:
                raise OrchestrationError("checkpoint predecessor link is broken")
        elif checkpoint.get("previous_checkpoint_fingerprint") is not None:
            raise OrchestrationError("first checkpoint cannot have a predecessor")
        sequence = len(index["checkpoints"])
        checkpoint_name = f"{sequence:04d}-{checkpoint['artifact_fingerprint']}.json"
        _write_immutable(self.checkpoints_dir / checkpoint_name, checkpoint)
        updated = sign_artifact(
            "CheckpointHistoryIndex",
            {
                "index_schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": run_id,
                "checkpoints": [*index["checkpoints"], checkpoint_name],
                "last_checkpoint_fingerprint": checkpoint["artifact_fingerprint"],
            },
        )
        _atomic_write_json(self.checkpoint_index_path, updated)

    def verify_checkpoint_history(self) -> list[dict[str, Any]]:
        """Verify immutable checkpoint copies and that the mutable checkpoint is their tip."""
        index = self._load_checkpoint_index()
        checkpoints: list[dict[str, Any]] = []
        previous: str | None = None
        for sequence, name in enumerate(index["checkpoints"]):
            path = self.checkpoints_dir / name
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                validate_signed_artifact(value, kind="BakeoffCheckpoint")
            except (OSError, json.JSONDecodeError, BakeoffContractError) as exc:
                raise OrchestrationError(f"checkpoint {name} is invalid") from exc
            expected_name = f"{sequence:04d}-{value['artifact_fingerprint']}.json"
            if name != expected_name:
                raise OrchestrationError("checkpoint history filename is not content-addressed")
            if value.get("previous_checkpoint_fingerprint") != previous:
                raise OrchestrationError("checkpoint history predecessor link is broken")
            if index["run_id"] not in (None, value.get("run_id")):
                raise OrchestrationError("checkpoint history run ID mismatch")
            previous = value["artifact_fingerprint"]
            checkpoints.append(value)
        if index["checkpoints"] and index.get("last_checkpoint_fingerprint") != previous:
            raise OrchestrationError("checkpoint history tail fingerprint mismatch")
        if not checkpoints:
            raise OrchestrationError("checkpoint history is empty")
        try:
            current = self.machine.load()
        except BakeoffContractError as exc:
            raise OrchestrationError("current checkpoint is invalid") from exc
        if current["artifact_fingerprint"] != checkpoints[-1]["artifact_fingerprint"]:
            raise OrchestrationError("current checkpoint is not the history tip")
        return checkpoints

    def _record_receipt(
        self,
        checkpoint: Mapping[str, Any],
        *,
        target: RunState,
        evidence: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        index = self._load_receipt_index()
        run_id = checkpoint.get("run_id")
        if index["run_id"] not in (None, run_id):
            raise OrchestrationError("receipt index belongs to another run")
        receipt_number = len(index["receipts"])
        previous = None
        if index["receipts"]:
            previous_name = index["receipts"][-1]
            previous_value = _read_control_artifact(self.receipts_dir / previous_name)
            validate_signed_artifact(previous_value, kind="BakeoffReceipt")
            previous = previous_value["artifact_fingerprint"]
        evidence_fingerprints = {
            name: artifact["artifact_fingerprint"] for name, artifact in evidence.items()
        }
        receipt = sign_artifact(
            "BakeoffReceipt",
            {
                "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": run_id,
                "sequence": receipt_number,
                "state": target.value,
                "checkpoint_fingerprint": checkpoint["artifact_fingerprint"],
                "previous_receipt_fingerprint": previous,
                "evidence_fingerprints": evidence_fingerprints,
            },
        )
        receipt_name = f"{receipt_number:04d}-{receipt['artifact_fingerprint']}.json"
        receipt_path = self.receipts_dir / receipt_name
        _write_immutable(receipt_path, receipt)
        updated = {
            "index_schema_version": RECEIPT_SCHEMA_VERSION,
            "run_id": run_id,
            "receipts": [*index["receipts"], receipt_name],
            "last_receipt_fingerprint": receipt["artifact_fingerprint"],
        }
        updated = sign_artifact("ReceiptIndex", updated)
        _atomic_write_json(self.index_path, updated)
        return receipt

    def verify_receipt_chain(self) -> list[dict[str, Any]]:
        """Validate every immutable receipt and its predecessor links, newest last."""
        index = self._load_receipt_index()
        receipts: list[dict[str, Any]] = []
        previous: str | None = None
        checkpoint_history = self.verify_checkpoint_history()
        for sequence, name in enumerate(index["receipts"]):
            path = _artifact_path(self.run_dir, name)
            value = _read_control_artifact(path)
            try:
                validate_signed_artifact(value, kind="BakeoffReceipt")
            except BakeoffContractError as exc:
                raise OrchestrationError(
                    f"receipt {name} fingerprint or signature is invalid"
                ) from exc
            if value.get("sequence") != sequence:
                raise OrchestrationError("receipt sequence is not contiguous")
            if name != f"{sequence:04d}-{value['artifact_fingerprint']}.json":
                raise OrchestrationError("receipt filename is not content-addressed")
            if value.get("previous_receipt_fingerprint") != previous:
                raise OrchestrationError("receipt predecessor link is broken")
            if index["run_id"] not in (None, value.get("run_id")):
                raise OrchestrationError("receipt run ID mismatch")
            if (
                sequence >= len(checkpoint_history)
                or value.get("checkpoint_fingerprint")
                != checkpoint_history[sequence]["artifact_fingerprint"]
            ):
                raise OrchestrationError("receipt checkpoint history link is broken")
            previous = value["artifact_fingerprint"]
            receipts.append(value)
        if len(receipts) != len(checkpoint_history):
            raise OrchestrationError("receipt history does not cover every checkpoint")
        if index["receipts"] and index.get("last_receipt_fingerprint") != previous:
            raise OrchestrationError("receipt index tail fingerprint mismatch")
        return receipts

    def initialize(self, spec_path: Path) -> dict[str, Any]:
        """Validate and freeze a spec, initialize the state machine, and record receipt zero."""
        spec = self._load_spec(spec_path)
        with self._run_lock():
            if (
                self.spec_path.exists()
                or self.machine.checkpoint_path.exists()
                or self.index_path.exists()
                or self.checkpoint_index_path.exists()
            ):
                raise OrchestrationError("run is already initialized")
            # BakeoffStateMachine owns the canonical run-local bakeoff_spec.json.  Keep one
            # writer so a second spec copy cannot race that atomic write.
            checkpoint = self.machine.initialize(spec)
            self._record_checkpoint(checkpoint)
            self._record_receipt(
                checkpoint,
                target=RunState.SPEC_FROZEN,
                evidence={"spec": spec},
            )
            return {
                "run_id": spec["run_id"],
                "state": checkpoint["state"],
                "spec_fingerprint": spec["artifact_fingerprint"],
                "checkpoint_fingerprint": checkpoint["artifact_fingerprint"],
            }

    def advance(  # noqa: C901, PLR0912, PLR0915
        self,
        target: RunState | str,
        evidence: Mapping[str, Mapping[str, Any]] | Mapping[str, Any],
        *,
        spec_path: Path | None = None,
        winner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate one typed evidence transition and record its immutable receipt.

        ``evidence`` may be one artifact (a mapping containing ``kind``) or a name-to-artifact
        mapping.  ``winner`` is required only for the winner-specific BlindUnlock validator.
        """
        state = _state(target)
        spec = self._load_spec(spec_path)
        with self._run_lock():
            current = self.machine.load()
            self.verify_checkpoint_history()
            if current.get("spec_fingerprint") != spec["artifact_fingerprint"]:
                raise OrchestrationError("checkpoint is stale relative to the supplied BakeoffSpec")
            current_state = _state(current.get("state"))
            if current.get("run_id") != spec["run_id"]:
                raise OrchestrationError("checkpoint belongs to a different bakeoff run")
            if state not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
                raise OrchestrationError(
                    f"invalid transition {current.get('state')} -> {state.value}"
                )
            if isinstance(evidence, Mapping) and "kind" in evidence:
                evidence_map: dict[str, Mapping[str, Any]] = {"evidence": evidence}
            elif isinstance(evidence, Mapping):
                evidence_map = dict(evidence)
            else:
                raise OrchestrationError(
                    "evidence must be a signed artifact or mapping of artifacts"
                )
            if not evidence_map:
                raise OrchestrationError("transition requires evidence")
            if state is RunState.BLIND_UNLOCKED and winner is not None:
                evidence_map["development_winner"] = winner
            validated: dict[str, Mapping[str, Any]] = {}
            if state in {RunState.PROMOTED, RunState.RETAINED}:
                required_kinds = {
                    "PromotionDecision",
                    "DevelopmentWinner",
                    "BlindEvaluation",
                    "BlindUnlock",
                    "BlindQueryManifestReceipt",
                }
                if any(not isinstance(artifact, Mapping) for artifact in evidence_map.values()):
                    raise OrchestrationError("evidence entries must be signed artifact objects")
                by_kind = {
                    str(artifact.get("kind")): artifact for artifact in evidence_map.values()
                }
                if len(by_kind) != len(evidence_map) or set(by_kind) != required_kinds:
                    raise OrchestrationError(
                        "terminal transition requires the complete custody evidence chain"
                    )
                terminal_winner = validate_development_winner(by_kind["DevelopmentWinner"], spec)
                terminal_unlock = validate_blind_unlock(
                    by_kind["BlindUnlock"], spec, terminal_winner
                )
                validate_blind_manifest_receipt(
                    by_kind["BlindQueryManifestReceipt"], spec, terminal_winner, terminal_unlock
                )
                validate_blind_evaluation(by_kind["BlindEvaluation"], spec)
                for name, artifact in evidence_map.items():
                    if artifact.get("kind") == "PromotionDecision":
                        validated[str(name)] = _validate_transition_artifact(
                            state, artifact, spec, terminal_winner
                        )
                    else:
                        validated[str(name)] = validate_signed_artifact(artifact)
            else:
                for name, artifact in evidence_map.items():
                    if not isinstance(artifact, Mapping):
                        raise OrchestrationError("evidence entries must be signed artifact objects")
                    if (
                        state is RunState.BLIND_UNLOCKED
                        and artifact.get("kind") == "DevelopmentWinner"
                    ):
                        validated[str(name)] = validate_development_winner(artifact, spec)
                    else:
                        validated[str(name)] = _validate_transition_artifact(
                            state, artifact, spec, winner
                        )
            expected_current_fingerprint = current["artifact_fingerprint"]
            if self.machine.load()["artifact_fingerprint"] != expected_current_fingerprint:
                raise OrchestrationError("checkpoint changed before compare-and-swap")
            try:
                checkpoint = self.machine.transition(
                    state,
                    validated,
                    expected_spec_fingerprint=spec["artifact_fingerprint"],
                )
            except BakeoffContractError as exc:
                raise OrchestrationError(str(exc)) from exc
            if checkpoint.get("previous_checkpoint_fingerprint") != expected_current_fingerprint:
                raise OrchestrationError("checkpoint compare-and-swap failed")
            self._record_checkpoint(checkpoint)
            receipt = self._record_receipt(checkpoint, target=state, evidence=validated)
            return {
                "run_id": spec["run_id"],
                "state": checkpoint["state"],
                "spec_fingerprint": spec["artifact_fingerprint"],
                "checkpoint_fingerprint": checkpoint["artifact_fingerprint"],
                "receipt_fingerprint": receipt["artifact_fingerprint"],
                "receipt_sequence": receipt["sequence"],
            }


def _parse_evidence_paths(values: Sequence[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if "=" in value:
            name, raw_path = value.split("=", 1)
        else:
            raw_path = value
            name = Path(raw_path).stem or "evidence"
        if not name or name in result:
            raise OrchestrationError("evidence names must be unique and non-empty")
        path = Path(raw_path)
        result[name] = _read_control_artifact(path)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    initialize = actions.add_parser("init", help="freeze a signed BakeoffSpec and initialize a run")
    initialize.add_argument("--run-dir", type=Path, required=True)
    initialize.add_argument("--spec", type=Path, required=True)
    advance = actions.add_parser("advance", help="advance one state using signed evidence")
    advance.add_argument("--run-dir", type=Path, required=True)
    advance.add_argument("--spec", type=Path)
    advance.add_argument("--target", choices=[state.value for state in RunState], required=True)
    advance.add_argument(
        "--evidence",
        action="append",
        required=True,
        help="Signed JSON evidence path; optionally NAME=PATH (repeatable).",
    )
    advance.add_argument(
        "--winner",
        type=Path,
        help="Signed DevelopmentWinner JSON, required when advancing to BLIND_UNLOCKED.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    orchestrator = BakeoffOrchestrator(args.run_dir)
    if args.action == "init":
        result = orchestrator.initialize(args.spec)
    else:
        winner = _read_control_artifact(args.winner) if args.winner else None
        result = orchestrator.advance(
            args.target,
            _parse_evidence_paths(args.evidence),
            spec_path=args.spec,
            winner=winner,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())


__all__ = [
    "BakeoffOrchestrator",
    "OrchestrationError",
    "RECEIPT_DIRNAME",
    "RECEIPT_INDEX_NAME",
    "SPEC_COPY_NAME",
    "main",
]
