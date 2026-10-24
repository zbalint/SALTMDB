"""Run exact search-memory outputs against a copied database snapshot.

Inputs are query JSON files and a daemon-created ``.db`` snapshot. The runner hashes and copies
that input to ``gate.db`` below the output directory, and opens only the copy. It emits one JSON
artifact containing complete ordered results, exceptions, provenance, liveness, and measured
latency. The comparer ignores latency and the four source-tree provenance fields only.

Usage::

    PYTHONHASHSEED=0 python search_equivalence_gate.py --db-path SNAPSHOT.db \
      --query-file queries.json --output-dir scratch/gate-out --expect-src /repo/src
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

MODEL_ENV_KEYS = ("SALTMDB_RERANKER_MODEL", "SALTMDB_ENABLE_SEMANTIC")
RERANKER_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_hashes(package_file: Path) -> dict[str, str]:
    root = package_file.resolve().parent / "models"
    hashes: dict[str, str] = {}
    if root.is_dir():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            hashes[str(path.relative_to(root))] = _sha256(path)
    return hashes


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("fastembed", "onnxruntime", "sqlite-vec"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "missing"
    return versions


def check_runtime() -> None:
    if sys.flags.isolated or sys.flags.ignore_environment:
        raise ValueError("isolated or ignore-environment interpreter is unsupported")
    if not os.environ.get("PYTHONHASHSEED"):
        raise ValueError("PYTHONHASHSEED must be set")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("ORT_INTRA_OP_NUM_THREADS", "1")
    os.environ.setdefault("ORT_INTER_OP_NUM_THREADS", "1")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def validate_db_path(path: Path, *, live_path: Path | None = None) -> Path:
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


def _validate_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    scratch_root = repo / "scratch"
    if not (scratch_root == resolved or scratch_root in resolved.parents):
        try:
            subprocess.run(
                ["git", "check-ignore", "--quiet", "--", str(resolved)],
                cwd=repo,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as error:
            if not str(resolved).startswith(str(Path(os.getenv("TMPDIR", "/tmp")).resolve())):
                raise ValueError(
                    "output directory must be gitignored scratch or a system temp path"
                ) from error
    return resolved


def _map_flat_arguments(search: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    if "criteria" not in inspect.signature(search).parameters:
        return dict(arguments)
    from saltmdb.domain.services.memory_service import SearchCriteria, SearchTuning

    criteria_names = (
        "context_id",
        "agent_session_id",
        "tags_filter",
        "tag_operator",
        "metadata_filter",
        "memory_type_filter",
        "is_core",
    )
    tuning_names = (
        "cross_encoder_candidate_cap",
        "cross_encoder_text_cap_chars",
        "use_chunk_candidates",
        "oversampling_multiplier",
        "candidate_window",
        "chunk_weight",
        "use_retrieval_text_candidates",
        "retrieval_fts_weight",
        "retrieval_vector_weight",
        "disable_semantic",
        "prefer_durable_types",
        "demote_superseded",
        "collapse_supersedes_families",
    )
    mapped = dict(arguments)
    criteria = {name: mapped.pop(name) for name in criteria_names if name in mapped}
    tuning = {name: mapped.pop(name) for name in tuning_names if name in mapped}
    if criteria:
        mapped["criteria"] = SearchCriteria(**criteria)
    if tuning:
        mapped["tuning"] = SearchTuning(**tuning)
    return mapped


def validate_arguments(search: Callable[..., Any], arguments: dict[str, Any]) -> None:
    effective = _map_flat_arguments(search, arguments)
    accepted = set(inspect.signature(search).parameters)
    unknown = sorted(set(effective) - accepted)
    if unknown:
        raise ValueError(f"unknown search_memory keyword(s): {', '.join(unknown)}")


def check_preflight(preflight: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    result = preflight(candidates=["equivalence preflight one", "equivalence preflight two"])
    scores = result.get("scores")
    ready = result.get("ready") is True
    if not ready or not isinstance(scores, list) or len(scores) != 2:
        raise ValueError("cross-encoder preflight failed")
    if any(
        isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
        for score in scores
    ):
        raise ValueError("cross-encoder preflight returned non-finite scores")
    return result


def _load_queries(paths: list[Path]) -> list[tuple[str, int, str]]:
    loaded: list[tuple[str, int, str]] = []
    for path in sorted(paths, key=lambda item: item.name):
        value = json.loads(path.read_text(encoding="utf-8"))
        entries = (
            value.get("queries", value.get("cases", value)) if isinstance(value, dict) else value
        )
        if not isinstance(entries, list):
            raise ValueError(f"query file {path} does not contain a list")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("query"), str):
                raise ValueError(f"query file {path} entry {index} lacks a string query")
            loaded.append((path.name, index, entry["query"]))
    return loaded


def _base_arguments(query: str, *, mode: str = "broad", limit: int = 10) -> dict[str, Any]:
    return {
        "agent_id": None,
        "query_keywords": query,
        "tags_filter": None,
        "metadata_filter": None,
        "explain_mode": False,
        "limit": limit,
        "context_id": None,
        "agent_session_id": None,
        "is_core": None,
        "memory_type_filter": None,
        "tag_operator": "AND",
        "cursor": None,
        "include_related": False,
        "prefer_durable_types": False,
        "demote_superseded": False,
        "cross_encoder_candidate_cap": None,
        "cross_encoder_text_cap_chars": None,
        "use_chunk_candidates": False,
        "oversampling_multiplier": None,
        "candidate_window": None,
        "chunk_weight": None,
        "collapse_supersedes_families": False,
        "return_diagnostics": False,
        "mode": mode,
        "disable_semantic": False,
        "use_retrieval_text_candidates": False,
        "retrieval_fts_weight": None,
        "retrieval_vector_weight": None,
        "db_connection": None,
    }


def _build_case(
    file_name: str,
    index: int,
    query: str,
    tier: str,
    name: str,
    mode: str,
    update: dict[str, Any],
    cross_encoder: bool,
) -> dict[str, Any]:
    arguments = _base_arguments(query, mode=mode)
    if tier == "B":
        arguments["limit"] = 5
        arguments["include_related"] = True
    arguments.update(update)
    environment = {"SALTMDB_RERANKER_MODEL": RERANKER_NAME} if cross_encoder else {}
    return {
        "key": [f"{tier}/{file_name}", index, name],
        "tier": tier,
        "query": query,
        "arguments": arguments,
        "environment": environment,
    }


TIER_A_SPECS: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    ("broad", "broad", {}, False),
    ("strict", "strict", {}, False),
    ("history", "history", {}, False),
    ("broad_ce", "broad", {}, True),
    ("strict_ce", "strict", {}, True),
    ("durable", "broad", {"prefer_durable_types": True, "demote_superseded": True}, False),
)

TIER_B_SPECS = (
    ("collapse", "broad", {"collapse_supersedes_families": True}, False),
    ("explain", "broad", {"explain_mode": True}, False),
    ("diagnostics", "broad", {"return_diagnostics": True}, False),
    (
        "chunks",
        "broad",
        {
            "use_chunk_candidates": True,
            "oversampling_multiplier": 8,
            "candidate_window": 40,
            "chunk_weight": 1.5,
        },
        False,
    ),
    ("chunks_default", "broad", {"use_chunk_candidates": True}, False),
    (
        "retrieval",
        "broad",
        {
            "use_retrieval_text_candidates": True,
            "retrieval_fts_weight": 0.7,
            "retrieval_vector_weight": 1.3,
            "return_diagnostics": True,
        },
        False,
    ),
    (
        "retrieval_default",
        "broad",
        {"use_retrieval_text_candidates": True, "return_diagnostics": True},
        False,
    ),
    (
        "ce_caps",
        "broad",
        {"cross_encoder_candidate_cap": 15, "cross_encoder_text_cap_chars": 1800},
        True,
    ),
    ("disable_semantic", "broad", {"disable_semantic": True}, False),
    ("invalid_cap", "broad", {"cross_encoder_candidate_cap": 11}, False),
    ("collapse_strict", "strict", {"collapse_supersedes_families": True}, False),
)


def _matrix_specs() -> list[tuple[str, str, dict[str, Any], bool]]:
    specs: list[tuple[str, str, dict[str, Any], bool]] = list(TIER_B_SPECS)
    for mode in ("broad", "history", "strict"):
        variants = (
            ((True, True),)
            if mode == "strict"
            else ((False, False), (True, False), (False, True), (True, True))
        )
        for prefer, demote in variants:
            for cross_encoder in (False, True):
                name = f"{mode}_{prefer}_{demote}_{'ce' if cross_encoder else 'noce'}"
                specs.append(
                    (
                        name,
                        mode,
                        {"prefer_durable_types": prefer, "demote_superseded": demote},
                        cross_encoder,
                    )
                )
    for mode in ("strict", "history"):
        for name, update in (
            ("explain", {"explain_mode": True}),
            ("diagnostics", {"return_diagnostics": True}),
        ):
            for cross_encoder in (False, True):
                specs.append(
                    (
                        f"{mode}_{name}_{'ce' if cross_encoder else 'noce'}",
                        mode,
                        update,
                        cross_encoder,
                    )
                )
    return specs


def _cursor_cases(queries: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for file_name, index, query in queries[::39]:
        for mode in ("broad", "strict"):
            for offset in (0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 57, 99, 198, 201, "x", "bad"):
                cursor = f"offset:{offset}" if offset != "bad" else "cursor-without-offset"
                arguments = _base_arguments(query, mode=mode, limit=3)
                arguments["cursor"] = cursor
                cases.append(
                    {
                        "key": ["C/" + file_name, index, f"{mode}_{offset}"],
                        "tier": "C",
                        "query": query,
                        "arguments": arguments,
                        "environment": {},
                    }
                )
    return cases


def _cases(queries: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for file_name, index, query in queries:
        cases.extend(_build_case(file_name, index, query, "A", *spec) for spec in TIER_A_SPECS)
    selected = queries[::9]
    for file_name, index, query in selected:
        cases.extend(_build_case(file_name, index, query, "B", *spec) for spec in _matrix_specs())
    cases.extend(_cursor_cases(queries))
    return cases


def _exact_probe_cases(seed: dict[str, Any], file_name: str, index: int) -> list[dict[str, Any]]:
    title = str(seed["title"])
    cases: list[dict[str, Any]] = []
    for title_index, exact_title in enumerate(seed.get("newest_titles", [title])):
        for mode in ("broad", "history", "strict"):
            arguments = _base_arguments(exact_title, mode=mode, limit=5)
            cases.append(
                _build_case(
                    file_name,
                    index,
                    exact_title,
                    "C",
                    f"exact_hit_{title_index}_{mode}",
                    mode,
                    arguments,
                    False,
                )
            )
        collapse_hit = _base_arguments(exact_title, mode="broad", limit=5)
        collapse_hit["collapse_supersedes_families"] = True
        cases.append(
            _build_case(
                file_name,
                index,
                exact_title,
                "C",
                f"exact_hit_{title_index}_collapse",
                "broad",
                collapse_hit,
                False,
            )
        )
    collision_title = seed.get("collision_title")
    if collision_title is not None:
        collision_args = _base_arguments(str(collision_title), limit=5)
        cases.append(
            _build_case(
                file_name,
                index,
                str(collision_title),
                "C",
                "exact_collision",
                "broad",
                collision_args,
                False,
            )
        )
    for mode in ("broad", "history", "strict"):
        arguments = _base_arguments(title, mode=mode, limit=0)
        arguments["cursor"] = "offset:3"
        cases.append(
            _build_case(file_name, index, title, "C", f"exact_title_{mode}", mode, arguments, False)
        )
    collapse = _base_arguments(title, mode="broad", limit=0)
    collapse["collapse_supersedes_families"] = True
    cases.append(
        _build_case(file_name, index, title, "C", "exact_title_collapse", "broad", collapse, False)
    )
    return cases


def _supersedes_cases(seed: dict[str, Any], file_name: str, index: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for edge_index, edge in enumerate(seed.get("supersedes", [])):
        keyword = " ".join(edge["newer_title"].split()[:5])
        for query_kind, text in (
            ("newer", edge["newer_title"]),
            ("older", edge["older_title"]),
            ("keyword", keyword),
        ):
            for mode in ("broad", "history"):
                for collapse in (False, True):
                    arguments = _base_arguments(text, mode=mode, limit=10)
                    arguments.update(
                        include_related=False,
                        collapse_supersedes_families=collapse,
                    )
                    cases.append(
                        _build_case(
                            file_name,
                            index,
                            text,
                            "C",
                            f"supersedes_{edge_index}_{query_kind}_{mode}_{collapse}",
                            mode,
                            arguments,
                            False,
                        )
                    )
            broad_demote = _base_arguments(text, mode="broad", limit=10)
            broad_demote.update(
                include_related=False,
                prefer_durable_types=True,
                demote_superseded=True,
            )
            cases.append(
                _build_case(
                    file_name,
                    index,
                    text,
                    "C",
                    f"supersedes_{edge_index}_{query_kind}_broad_demote_durable",
                    "broad",
                    broad_demote,
                    False,
                )
            )
            strict = _base_arguments(text, mode="strict", limit=10)
            strict["include_related"] = False
            cases.append(
                _build_case(
                    file_name,
                    index,
                    text,
                    "C",
                    f"supersedes_{edge_index}_{query_kind}_strict_demote",
                    "strict",
                    strict,
                    False,
                )
            )
    return cases


def _derived_cases(
    seed: dict[str, Any], queries: list[tuple[str, int, str]]
) -> list[dict[str, Any]]:
    if seed.get("status") == "empty" or not queries:
        return []
    file_name, index, query = queries[0]
    cases: list[dict[str, Any]] = _exact_probe_cases(seed, file_name, index)
    tags = seed.get("tags", [])
    if tags:
        for operator in ("AND", "OR"):
            arguments = _base_arguments(query, limit=3)
            arguments.update(tags_filter=[tags[0]], tag_operator=operator)
            cases.append(
                _build_case(
                    file_name, index, query, "C", f"tags_{operator}", "broad", arguments, False
                )
            )
    for operator in ("AND", "OR"):
        negative = _base_arguments(query, limit=3)
        negative.update(tags_filter=["__equivalence_missing_tag__"], tag_operator=operator)
        cases.append(
            _build_case(
                file_name, index, query, "C", f"tags_missing_{operator}", "broad", negative, False
            )
        )
    alias_tags = seed.get("alias_tags", [])
    if alias_tags:
        alias = _base_arguments(query, limit=3)
        alias["tags_filter"] = [alias_tags[0]]
        cases.append(_build_case(file_name, index, query, "C", "tags_alias", "broad", alias, False))
    for name, field, value in (
        ("memory_type", "memory_type_filter", seed.get("memory_type", "fact")),
        ("agent", "agent_id", seed.get("agent_id")),
        ("context", "context_id", seed.get("context_id")),
        ("session", "agent_session_id", seed.get("agent_session_id")),
        ("core", "is_core", seed.get("is_core")),
    ):
        if value is not None:
            arguments = _base_arguments(query, limit=3)
            arguments[field] = value
            cases.append(
                _build_case(
                    file_name, index, query, "C", f"filter_{name}", "broad", arguments, False
                )
            )
    metadata = _base_arguments(query, limit=3)
    metadata["metadata_filter"] = seed.get("metadata", {})
    cases.append(
        _build_case(file_name, index, query, "C", "metadata_valid", "broad", metadata, False)
    )
    invalid = _base_arguments(query, limit=3)
    invalid["metadata_filter"] = {"__missing_equivalence_key__": "__missing__"}
    cases.append(
        _build_case(file_name, index, query, "C", "metadata_invalid", "broad", invalid, False)
    )
    browse_filters = (
        ("tags", {"tags_filter": [tags[0]] if tags else ["__equivalence_missing_tag__"]}),
        ("memory_type", {"memory_type_filter": seed.get("memory_type", "fact")}),
        ("agent", {"agent_id": seed.get("agent_id")}),
        ("context", {"context_id": seed.get("context_id")}),
        ("session", {"agent_session_id": seed.get("agent_session_id")}),
        ("core", {"is_core": seed.get("is_core")}),
        ("metadata", {"metadata_filter": seed.get("metadata", {})}),
    )
    for name, updates in browse_filters:
        if any(value is not None for value in updates.values()):
            filtered_browse = _base_arguments("", mode="broad", limit=3)
            filtered_browse["query_keywords"] = None
            filtered_browse.update(updates)
            cases.append(
                _build_case(
                    file_name, index, query, "C", f"browse_{name}", "broad", filtered_browse, False
                )
            )
    for cursor in ("cursor-without-offset", "offset:bad"):
        malformed = _base_arguments(query, limit=3)
        malformed["cursor"] = cursor
        cases.append(
            _build_case(file_name, index, query, "C", f"cursor_{cursor}", "broad", malformed, False)
        )
    diagnostics = _base_arguments(query, limit=3)
    diagnostics["return_diagnostics"] = True
    cases.append(
        _build_case(
            file_name, index, query, "C", "reranker_unset_diagnostics", "broad", diagnostics, False
        )
    )
    semantic_off = _build_case(
        file_name,
        index,
        query,
        "C",
        "semantic_off",
        "broad",
        _base_arguments(query, limit=3),
        False,
    )
    semantic_off["environment"] = {"SALTMDB_ENABLE_SEMANTIC": "false"}
    cases.append(semantic_off)
    cases.extend(_supersedes_cases(seed, file_name, index))
    return cases


def _source_header(expect_src: Path, runner_path: Path, input_hash: str) -> dict[str, Any]:
    import saltmdb

    source_file = Path(saltmdb.__file__).resolve()
    source_root = source_file.parents[1]
    if source_root != expect_src.resolve():
        raise ValueError(
            f"imported saltmdb source {source_root} does not match --expect-src {expect_src.resolve()}"
        )
    tree = source_root.parent
    status_full = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tree, check=True, capture_output=True, text=True
    ).stdout
    status_src = subprocess.run(
        ["git", "status", "--porcelain", "--", "src"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tree, check=True, capture_output=True, text=True
    ).stdout.strip()
    environment = dict(
        sorted(
            (key, value)
            for key, value in os.environ.items()
            if key.startswith(("SALTMDB_", "OMP_", "ORT_")) or key == "PYTHONHASHSEED"
        )
    )
    return {
        "src_realpath": str(source_file),
        "src_head": head,
        "src_status_full": status_full,
        "src_status_src": status_src,
        "runner_sha256": _sha256(runner_path),
        "input_sha256": input_hash,
        "environment": environment,
        "cpu_count": os.cpu_count(),
        "package_versions": _package_versions(),
        "model_sha256": _model_hashes(source_file),
    }


def _snapshot_seed(db_path: Path) -> dict[str, Any]:
    from saltmdb.db.connection import open_read_connection

    connection = open_read_connection(str(db_path))
    try:
        rows = connection.execute(
            "SELECT id, title, agent_id, context_id, agent_session_id, is_core, "
            "memory_type, metadata FROM entities WHERE status = 'raw' "
            "ORDER BY updated_at DESC, id LIMIT 3"
        ).fetchall()
        if not rows:
            return {"status": "empty"}
        row = rows[0]
        entity_id, title, agent_id, context_id, session_id, is_core, memory_type, metadata_text = (
            row
        )
        newest_titles = [str(item[1]) for item in rows]
        collision = connection.execute(
            "SELECT title FROM entities WHERE status = 'raw' GROUP BY title "
            "HAVING COUNT(*) > 1 ORDER BY title LIMIT 1"
        ).fetchone()
        metadata = json.loads(metadata_text) if isinstance(metadata_text, str) else {}
        tag_rows = connection.execute(
            "SELECT tags.name FROM tags JOIN entity_tags ON entity_tags.tag_id = tags.id "
            "WHERE entity_tags.entity_id = ? ORDER BY tags.name",
            (entity_id,),
        ).fetchall()
        tags = [str(tag[0]) for tag in tag_rows]
        alias_rows = connection.execute(
            "SELECT name FROM tags WHERE canonical_id IS NOT NULL ORDER BY name"
        ).fetchall()
        aliases = [str(alias[0]) for alias in alias_rows]
        relation_rows = connection.execute(
            "SELECT r.id, newer.title, older.title FROM relations AS r "
            "JOIN entities AS newer ON newer.id = r.source_id "
            "JOIN entities AS older ON older.id = r.target_id "
            "WHERE r.predicate = 'supersedes' "
            "AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime('now')) "
            "AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now')) "
            "AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime('now')) "
            "AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime('now')) "
            "AND newer.status != 'archived' AND older.status != 'archived' "
            "ORDER BY r.id LIMIT 3"
        ).fetchall()
        supersedes = [
            {"id": relation[0], "newer_title": str(relation[1]), "older_title": str(relation[2])}
            for relation in relation_rows
        ]
        return {
            "entity_id": entity_id,
            "title": title,
            "agent_id": agent_id,
            "context_id": context_id,
            "agent_session_id": session_id,
            "is_core": bool(is_core),
            "memory_type": memory_type,
            "metadata": metadata,
            "tags": tags,
            "newest_titles": newest_titles,
            "collision_title": str(collision[0]) if collision else None,
            "collision_status": None if collision else "skipped: none in corpus",
            "alias_tags": aliases,
            "supersedes": supersedes,
            "supersedes_status": None if supersedes else "skipped: none in corpus",
            "alias_status": None if aliases else "skipped: none in corpus",
        }
    finally:
        connection.close()


def _call(
    search: Callable[..., Any],
    arguments: dict[str, Any],
    environment: dict[str, str],
    db_path: Path,
) -> dict[str, Any]:
    original = {key: os.environ.get(key) for key in MODEL_ENV_KEYS}
    try:
        for key in MODEL_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(environment)
        call_arguments = dict(arguments)
        call_arguments["db_path"] = str(db_path)
        started = time.perf_counter()
        try:
            output = search(**_map_flat_arguments(search, call_arguments))
            result: dict[str, Any] = {"output": output}
        except Exception as error:  # per-call exceptions are part of the compared contract
            result = {
                "output": {"exception": {"type": type(error).__name__, "message": str(error)}}
            }
        result["elapsed_seconds"] = time.perf_counter() - started
        return result
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _tier_timing(cases: list[dict[str, Any]]) -> dict[str, list[float]]:
    timing: dict[str, list[float]] = {"A": [], "B": [], "C": []}
    for case in cases:
        tier = case.get("tier")
        elapsed = case.get("elapsed_seconds")
        if tier in timing and isinstance(elapsed, (int, float)):
            timing[tier].append(float(elapsed))
    return timing


def _print_extrapolation(cases: list[dict[str, Any]]) -> None:
    for tier, values in _tier_timing(cases).items():
        if len(values) >= 20:
            average = sum(values[:20]) / 20
            total = sum(1 for case in cases if case.get("tier") == tier)
            print(
                f"tier {tier}: sampled 20 calls; extrapolated {average * total:.1f}s for {total} calls"
            )


def _has_liveness(cases: list[dict[str, Any]]) -> dict[str, bool]:
    cross_encoder = False
    preview = False
    semantic = False
    for case in cases:
        output = case.get("output")
        if not isinstance(output, dict):
            continue
        diagnostics = output.get("diagnostics", {})
        if isinstance(diagnostics, dict):
            cross_encoder |= bool(
                isinstance(diagnostics.get("cross_encoder"), dict)
                and diagnostics["cross_encoder"].get("executed")
            )
            semantic |= bool(diagnostics.get("in_semantic"))
            candidates = diagnostics.get("candidates", [])
            if isinstance(candidates, list):
                semantic |= any(
                    isinstance(item, dict) and item.get("in_semantic") is True
                    for item in candidates
                )
        payload = output.get("results", output)
        if isinstance(payload, dict):
            preview |= "relevance_preview" in payload
            semantic |= bool(payload.get("in_semantic"))
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    preview |= "relevance_preview" in item
                    semantic |= item.get("in_semantic") is True
    return {"cross_encoder": cross_encoder, "preview": preview, "semantic": semantic}


def _resume_cases(
    destination: Path,
    header: dict[str, Any],
    current_cases: list[dict[str, Any]] | set[tuple[Any, ...]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    if not destination.exists():
        return {}
    previous = json.loads(destination.read_text(encoding="utf-8"))
    previous_header = previous.get("header", {})
    fields = (
        "input_sha256",
        "runner_sha256",
        "src_head",
        "src_status_src",
        "environment",
        "model_sha256",
        "package_versions",
    )
    if not all(previous_header.get(field) == header.get(field) for field in fields):
        return {}
    expected: dict[tuple[Any, ...], dict[str, Any] | None]
    if isinstance(current_cases, set):
        expected = {key: None for key in current_cases}
    else:
        expected = {tuple(case["key"]): case for case in current_cases}
    completed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for case in previous.get("cases", []):
        if not isinstance(case, dict) or not isinstance(case.get("key"), list):
            continue
        key = tuple(case["key"])
        expected_case = expected.get(key)
        if expected_case is None and key not in expected:
            continue
        if expected_case is not None and any(
            case.get(field) != expected_case.get(field)
            for field in ("query", "arguments", "environment")
        ):
            continue
        completed[key] = case
    return completed


def _write_partial(
    destination: Path,
    header: dict[str, Any],
    preflight: dict[str, Any],
    cases: list[dict[str, Any]],
) -> None:
    artifact = {
        "header": header,
        "complete": False,
        "liveness": {"preflight": bool(preflight.get("ready"))},
        "cases": cases,
    }
    destination.write_text(
        json.dumps(artifact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def _execute_cases(
    cases: list[dict[str, Any]],
    completed: dict[tuple[Any, ...], dict[str, Any]],
    artifact_cases: list[dict[str, Any]],
    search: Callable[..., Any],
    copied_db: Path,
    destination: Path,
    header: dict[str, Any],
    preflight: dict[str, Any],
    validate: bool = False,
) -> None:
    counts: dict[str, int] = {}
    for case in cases:
        key = tuple(case["key"])
        if key in completed:
            continue
        if validate:
            validate_arguments(search, case["arguments"] | {"db_path": str(copied_db)})
        completed[key] = {
            **case,
            **_call(search, case["arguments"], case["environment"], copied_db),
        }
        artifact_cases.append(completed[key])
        _write_partial(destination, header, preflight, artifact_cases)
        tier = str(case.get("tier", "?"))
        counts[tier] = counts.get(tier, 0) + 1
        print(f"progress {tier} {counts[tier]}")


def _sample_cases(cases: list[dict[str, Any]], per_tier: int = 20) -> list[dict[str, Any]]:
    counts = {"A": 0, "B": 0, "C": 0}
    sample: list[dict[str, Any]] = []
    for case in cases:
        tier = case.get("tier")
        if tier in counts and counts[tier] < per_tier:
            sample.append(case)
            counts[tier] += 1
    return sample


def _validate_case_arguments(
    cases: list[dict[str, Any]], search: Callable[..., Any], copied_db: Path
) -> None:
    for case in cases:
        validate_arguments(search, case["arguments"] | {"db_path": str(copied_db)})


def _run_preflight(preflight: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    original = os.environ.get("SALTMDB_RERANKER_MODEL")
    os.environ["SALTMDB_RERANKER_MODEL"] = RERANKER_NAME
    try:
        return check_preflight(preflight)
    finally:
        if original is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = original


def run(args: argparse.Namespace) -> Path:
    check_runtime()
    from saltmdb.config import get_db_path
    from saltmdb.domain.services.memory_service import search_memory
    from saltmdb.domain.services.reranker_service import score_pairs_preflight

    source_db = validate_db_path(Path(args.db_path), live_path=Path(get_db_path()))
    output_dir = _validate_output_dir(Path(args.output_dir))
    copied_db = output_dir / "gate.db"
    shutil.copy2(source_db, copied_db)
    input_hash = _sha256(source_db)
    queries = _load_queries([Path(path) for path in args.query_file])
    if args.max_queries is not None:
        queries = queries[: args.max_queries]
    cases = _cases(queries)
    seed = _snapshot_seed(copied_db)
    derived_cases: list[dict[str, Any]] = []
    for query_row in queries[::39][:10]:
        derived_cases.extend(_derived_cases(seed, [query_row]))
    all_cases = cases + derived_cases
    _validate_case_arguments(all_cases, search_memory, copied_db)
    preflight = _run_preflight(score_pairs_preflight)
    header = _source_header(Path(args.expect_src), Path(__file__).resolve(), input_hash)
    header["probe_seed"] = seed
    destination = output_dir / f"{args.run_name}.json"
    completed = _resume_cases(destination, header, all_cases)
    artifact_cases = list(completed.values())
    sample = _sample_cases(all_cases)
    _execute_cases(
        sample,
        completed,
        artifact_cases,
        search_memory,
        copied_db,
        destination,
        header,
        preflight,
        validate=True,
    )
    _print_extrapolation(artifact_cases)
    if args.sample_only:
        _write_partial(destination, header, preflight, artifact_cases)
        print(f"sample-only wrote {destination} ({len(artifact_cases)} cases)")
        return destination
    remaining = [case for case in all_cases if tuple(case["key"]) not in completed]
    _execute_cases(
        remaining,
        completed,
        artifact_cases,
        search_memory,
        copied_db,
        destination,
        header,
        preflight,
        validate=True,
    )
    live = _has_liveness(artifact_cases)
    liveness = {"preflight": bool(preflight.get("ready")), **live}
    if not all(liveness.values()):
        raise ValueError(f"runtime liveness checks failed: {liveness}")
    artifact = {
        "header": header,
        "probe_report": {"alias_status": seed.get("alias_status")},
        "complete": True,
        "liveness": liveness,
        "cases": artifact_cases,
    }
    destination.write_text(
        json.dumps(artifact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"wrote {destination} ({len(artifact_cases)} cases)")
    return destination


def main(argv: list[str] | None = None) -> int:
    if sys.flags.isolated or sys.flags.ignore_environment:
        print("isolated or ignore-environment interpreter is unsupported", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--query-file", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expect-src", required=True)
    parser.add_argument("--run-name", default="run")
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--sample-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(args)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"gate refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
