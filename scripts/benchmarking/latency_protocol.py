"""Declarative latency protocol for Stage-1 search evaluation.

The protocol records the measurement contract; it does not start a daemon or benchmark the
service.  Actual matrix runners may consume the metadata later.  Keeping the protocol separate
also makes it explicit that direct-service calls are diagnostic-only and cannot be mixed into a
promotion result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from evaluation_artifacts import artifact_fingerprint


LATENCY_PROTOCOL_VERSION = "stage1-daemon-warm-v1"
WARMUP_COUNT = 20
INTERLEAVED_REPETITIONS = 5
P95_LIMIT_SECONDS = 1.0
MAX_SLOWDOWN_FRACTION = 0.15


@dataclass(frozen=True)
class LatencyProtocol:
    protocol_version: str = LATENCY_PROTOCOL_VERSION
    daemon_mode: str = "persistent"
    corpus_locked: bool = True
    machine_locked: bool = True
    warmups: int = WARMUP_COUNT
    interleaved_repetitions: int = INTERLEAVED_REPETITIONS
    direct_service_diagnostic_only: bool = True
    p95_limit_seconds: float = P95_LIMIT_SECONDS
    max_slowdown_fraction: float = MAX_SLOWDOWN_FRACTION

    def to_dict(self) -> dict:
        value = asdict(self)
        value["fingerprint"] = artifact_fingerprint(value)
        return value


def validate_latency_protocol(value: Mapping[str, object]) -> dict:
    """Validate exact warm-daemon measurement metadata and return a detached dict."""
    if not isinstance(value, Mapping):
        raise ValueError("latency protocol must be an object")
    expected = asdict(LatencyProtocol())
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"latency protocol {key} must be {expected_value!r}")
    stored = value.get("fingerprint")
    unsigned = dict(value)
    unsigned.pop("fingerprint", None)
    if not isinstance(stored, str) or stored != artifact_fingerprint(unsigned):
        raise ValueError("latency protocol fingerprint mismatch")
    return dict(value)


def build_latency_protocol(
    *,
    corpus_fingerprint: str,
    machine_fingerprint: str,
    daemon_id: str | None = None,
) -> dict:
    """Build protocol metadata bound to one corpus/machine without doing measurements."""
    corpus_fingerprint = str(corpus_fingerprint).strip()
    machine_fingerprint = str(machine_fingerprint).strip()
    if not corpus_fingerprint or not machine_fingerprint:
        raise ValueError("latency protocol requires corpus and machine fingerprints")
    value = LatencyProtocol().to_dict()
    value.update(
        {
            "corpus_fingerprint": corpus_fingerprint,
            "machine_fingerprint": machine_fingerprint,
        }
    )
    if daemon_id is not None:
        if not str(daemon_id).strip():
            raise ValueError("daemon_id must be non-empty when supplied")
        value["daemon_id"] = str(daemon_id)
    # Fingerprint includes the corpus/machine binding as well as the protocol constants.
    unsigned = dict(value)
    unsigned.pop("fingerprint", None)
    value["fingerprint"] = artifact_fingerprint(unsigned)
    return value


def interleaved_schedule(
    config_names: list[str], repetitions: int = INTERLEAVED_REPETITIONS
) -> list[str]:
    """Return deterministic config-major interleaving for warm latency repetitions.

    The schedule is round-robin (one request per config in each repetition), avoiding a run where
    one contender receives all cold/cache-adjacent calls.  It is a helper for the eventual daemon
    runner and has no direct-service side effects.
    """
    if repetitions != INTERLEAVED_REPETITIONS:
        raise ValueError("Stage-1 latency protocol requires exactly five repetitions")
    names = [str(name) for name in config_names]
    if not names or len(names) != len(set(names)):
        raise ValueError("config_names must be a non-empty unique list")
    return [name for _ in range(repetitions) for name in names]


def latency_metadata(
    *,
    corpus_fingerprint: str,
    machine_fingerprint: str,
    daemon_id: str,
) -> dict:
    """Alias with a descriptive name used by matrix/reporting callers."""
    return build_latency_protocol(
        corpus_fingerprint=corpus_fingerprint,
        machine_fingerprint=machine_fingerprint,
        daemon_id=daemon_id,
    )
