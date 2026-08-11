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
from pathlib import Path

_SHARED_MODULE_PATH = Path(__file__).parent / "benchmark_search_option_matrix.py"


def _load_shared_build_configs():
    spec = importlib.util.spec_from_file_location(
        "benchmark_search_option_matrix", _SHARED_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._build_configs


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
            # New reference config (§0b item 3): what mode="history" callers actually get today,
            # matching search_memory's real post-1be6770 defaults.
            "name": "history_current_default",
            "mode": "history",
            "rerank_by_topic": False,
            "prefer_durable_types": True,
            "demote_superseded": True,
            "use_cross_encoder": False,
        },
    ]

    result = broad_and_strict + history_configs
    assert len(result) == 24, f"expected 24 total configs, got {len(result)}"
    names = [cfg["name"] for cfg in result]
    assert len(names) == len(set(names)), f"duplicate config names: {names}"
    return result


# The plan's own name for "today's actual current default" -- both the broad-mode config AND
# the value used throughout §5's decision rule (comparisons are always vs. this config).
CURRENT_DEFAULT_CONFIG_NAME = "broad_rt0_pdt1_ds1_ce0"
