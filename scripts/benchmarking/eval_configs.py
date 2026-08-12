"""Owns the 24-config candidate matrix for the precision-first search evaluation
(`scratch/plans/precision_first_search_evaluation.md`, §0b item 15 / §3). Imports the shared
`benchmark_search_option_matrix.py::_build_configs()` for its 16 broad + 5 strict-reference
configs UNCHANGED, then builds its own 3 history-reference configs directly -- per the plan's
explicit round-4 fix, this module NEVER modifies the shared file (which other, unrelated
benchmark runs still depend on in its original 2-history-config shape).

24 = 16 broad + 5 strict-reference + 3 history-reference
   (`history_all_false`, `history_kitchen_sink`, `history_current_default`)
"""

import importlib.util
import hashlib
import json
from pathlib import Path

_SHARED_MODULE_PATH = Path(__file__).parent / "benchmark_search_option_matrix.py"


# These controls are deliberately represented in the evaluation manifest before any runtime
# implementation exists.  Every default is the actual broad-mode runtime baseline: the current
# search service leaves all optional ranking/candidate/family/retrieval-text behavior disabled.
# Keeping the controls in the signed config shape prevents a future implementation from being
# benchmarked under an ambiguous "missing field means old default" interpretation.
FUTURE_CONTROL_DEFAULTS = {
    "use_chunk_candidates": False,
    "oversampling_multiplier": 1,
    "candidate_window": 0,
    "chunk_weight": 0.0,
    "collapse_supersedes_families": False,
    "cross_encoder_candidate_cap": None,
    "cross_encoder_text_cap_chars": None,
    "force_cross_encoder": False,
    "use_retrieval_text_candidates": False,
    "retrieval_fts_weight": 0.0,
    "retrieval_vector_weight": 0.0,
}


RUNTIME_BASELINE_CONFIG_NAME = "broad_rt0_pdt0_ds0_ce0"


def _load_shared_build_configs():
    spec = importlib.util.spec_from_file_location(
        "benchmark_search_option_matrix", _SHARED_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._build_configs


def _with_future_controls(config: dict) -> dict:
    """Return a detached config carrying the Stage-1 future-control metadata."""
    result = dict(config)
    for key, value in FUTURE_CONTROL_DEFAULTS.items():
        result.setdefault(key, value)
    # This flag is descriptive only.  It makes it possible to audit the frozen baseline without
    # inferring it from a config name, while keeping the historical 24-config matrix shape.
    result["is_runtime_baseline"] = result["name"] == RUNTIME_BASELINE_CONFIG_NAME
    return result


def validate_config(config: dict) -> None:
    """Validate the signed config shape without executing any future runtime controls."""
    if not isinstance(config, dict) or not isinstance(config.get("name"), str):
        raise ValueError("config must have a non-empty name")
    for key in (
        "mode",
        "rerank_by_topic",
        "prefer_durable_types",
        "demote_superseded",
        "use_cross_encoder",
    ):
        if key not in config:
            raise ValueError(f"config missing {key}")
    if config["mode"] not in {"broad", "strict", "history"}:
        raise ValueError("config mode is invalid")
    for key in (
        "rerank_by_topic",
        "prefer_durable_types",
        "demote_superseded",
        "use_cross_encoder",
        "use_chunk_candidates",
        "collapse_supersedes_families",
        "force_cross_encoder",
        "use_retrieval_text_candidates",
    ):
        if not isinstance(config.get(key), bool):
            raise ValueError(f"config {key} must be boolean")
    for key in (
        "oversampling_multiplier",
        "candidate_window",
        "chunk_weight",
        "retrieval_fts_weight",
        "retrieval_vector_weight",
    ):
        if not isinstance(config.get(key), (int, float)) or isinstance(config.get(key), bool):
            raise ValueError(f"config {key} must be numeric")
        if config[key] < 0:
            raise ValueError(f"config {key} cannot be negative")
    for key in ("cross_encoder_candidate_cap", "cross_encoder_text_cap_chars"):
        value = config.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ValueError(f"config {key} must be a positive integer or null")


def config_fingerprint(configs: list[dict] | None = None) -> str:
    """Stable fingerprint for a config manifest, including future controls."""
    values = _build_evaluation_configs() if configs is None else configs
    for config in values:
        validate_config(config)
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _build_evaluation_configs() -> list[dict]:
    """Returns the plan's 24-config matrix. Filters the shared builder's output down to its
    broad (16) and strict (5) configs unchanged; discards the shared builder's own 2 history
    configs entirely (one of which, `history_default`, is mislabeled relative to today's actual
    `search_memory` default -- §0b item 3) and replaces them with 3 explicitly-named ones here."""
    shared_build_configs = _load_shared_build_configs()
    shared_configs = shared_build_configs()

    broad_and_strict = [cfg for cfg in shared_configs if cfg["mode"] in ("broad", "strict")]
    assert len(broad_and_strict) == 21, (
        f"expected 16 broad + 5 strict = 21 configs from the shared builder, got "
        f"{len(broad_and_strict)} -- shared _build_configs() may have changed shape; "
        f"re-verify against scripts/benchmarking/benchmark_search_option_matrix.py before "
        f"trusting this evaluation's config manifest."
    )

    history_configs = [
        {
            # Relabeled from the shared builder's "history_default" -- that name is mislabeled
            # relative to today's actual search_memory default (§0b item 3): these flags
            # (pdt=False, ds=False) are the OLD pre-1be6770 declared-parameter default, not what
            # mode="history" callers get today.
            "name": "history_all_false",
            "mode": "history",
            "rerank_by_topic": False,
            "prefer_durable_types": False,
            "demote_superseded": False,
            "use_cross_encoder": False,
        },
        {
            "name": "history_kitchen_sink",
            "mode": "history",
            "rerank_by_topic": True,
            "prefer_durable_types": True,
            "demote_superseded": True,
            "use_cross_encoder": True,
        },
        {
            # Historical status-quo reference for the frozen 2026-08-11 evaluation. It records
            # the defaults that were active when the development shortlist and blind comparison
            # family were signed; it must not track a later runtime-default rollout.
            "name": "history_current_default",
            "mode": "history",
            "rerank_by_topic": False,
            "prefer_durable_types": True,
            "demote_superseded": True,
            "use_cross_encoder": False,
        },
    ]

    result = [_with_future_controls(config) for config in broad_and_strict + history_configs]
    assert len(result) == 24, f"expected 24 total configs, got {len(result)}"
    names = [cfg["name"] for cfg in result]
    assert len(names) == len(set(names)), f"duplicate config names: {names}"
    for config in result:
        validate_config(config)
    return result


# The frozen evaluation's status-quo baseline, used throughout its signed development/blind
# comparison family. It intentionally remains the pre-rollout configuration so historical
# shortlist and analysis artifacts stay tamper-valid after runtime defaults change.
CURRENT_DEFAULT_CONFIG_NAME = "broad_rt0_pdt1_ds1_ce0"

# ``CURRENT_DEFAULT_CONFIG_NAME`` remains the status-quo name embedded in the completed Luna
# evaluation artifacts.  New Stage-1 runs must use this explicit runtime baseline instead; the
# distinction prevents old shortlist/decision files from being reinterpreted after the 2026-08-12
# broad-default flip.
LEGACY_FROZEN_EVALUATION_BASELINE_CONFIG_NAME = CURRENT_DEFAULT_CONFIG_NAME


def runtime_baseline_config() -> dict:
    """Return the actual broad runtime baseline with every optional Stage-1 control disabled."""
    configs = _build_evaluation_configs()
    baseline = next(
        (config for config in configs if config["name"] == RUNTIME_BASELINE_CONFIG_NAME), None
    )
    if baseline is None:
        raise ValueError(f"runtime baseline {RUNTIME_BASELINE_CONFIG_NAME!r} is missing")
    expected_false = (
        "rerank_by_topic",
        "prefer_durable_types",
        "demote_superseded",
        "use_cross_encoder",
        "use_chunk_candidates",
        "collapse_supersedes_families",
        "force_cross_encoder",
        "use_retrieval_text_candidates",
    )
    if baseline["mode"] != "broad" or any(baseline[key] for key in expected_false):
        raise ValueError("runtime baseline is not the all-disabled broad configuration")
    return dict(baseline)
