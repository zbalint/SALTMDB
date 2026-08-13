"""Build the pooled Gate-D judging matrix from the 9 signed ``RetrievalRunBundle``s.

Gate D's contender execution stage (``run_retrieval_bakeoff.py``) produces one signed
``RetrievalRunBundle`` per contender, each carrying a per-query top-20 ranked hit list.  Judging
(``judge_pool.build_judge_packets``) does not evaluate those bundles directly -- it needs a single
pre-pooled, deduplicated ``matrix.pools`` structure: for every dev query, one candidate pool made
of real document text, so a judge grades documents rather than raw ranked IDs.  This script is the
missing bridge between the two: it reads the frozen ``BakeoffSpec``, all 9 ``RetrievalRunBundle``s,
the signed ``CorpusRepresentationManifest``, the unsigned ``corpus_export.json`` (hash-verified
against the manifest before any of its text is trusted), and the 400 dev queries, and emits one
signed ``JudgingMatrix`` artifact.

Field-by-field derivation
--------------------------
``pools``
    For every dev query, the union of every contender's top-20 ``entity_id``s (deduplicated),
    plus the query's own ``source_entity_ids`` force-included even when no contender's top-20
    happened to surface them.  This mirrors the older search-option-matrix track's
    "contender-union top-20 pool with forced source inclusion" design principle (see
    ``run_evaluation_matrix.py``'s pool-building code, referenced for shape only -- that module
    belongs to an unrelated track with a different query/candidate schema and is never imported
    here) and the documented Gate-D judging-pool design (SALTMDB memory
    ``5329dfb4-5665-4f01-9eec-1774df8155ce``: "builds contender-union top-20 pools").  Each pooled
    entry is ``{"title": ..., "full_content": <body>, "ground_truth_forced_include": bool}`` --
    ``full_content`` (not ``snippet``) because this is the full frozen entity body text, not a
    truncated excerpt; truncation to an excerpt happens later, inside
    ``judge_pool.build_query_centered_excerpt``, not here.

``spec_fingerprint`` / ``corpus_root_hash``
    Read verbatim off the already-validated signed ``BakeoffSpec``/``CorpusRepresentationManifest``
    (never recomputed or hardcoded), so any downstream consumer can confirm this matrix was built
    against the exact frozen inputs it claims.

``contenders``
    The sorted set of contender IDs actually present across the 9 loaded bundles (which
    ``load_bundles`` has already confirmed equals ``spec["contenders"]`` exactly) -- recorded here
    too so the matrix is self-describing without requiring a reader to re-open the spec.

``query_count`` / ``pool_top_n``
    The real, observed dev query count (400) and the pooling depth actually used (default 20,
    matching every contender's top-20 result cap -- there is nothing deeper to pool from a
    ``RetrievalRunBundle`` that only ever stores 20 hits per query).

There is no ``bakeoff_state.validate_judging_matrix`` -- that module's schema surface is
intentionally out of scope for this task (see the task's own hard constraints).  This script signs
the matrix with the generic, already-fingerprinted ``bakeoff_state.sign_artifact`` instead, exactly
like ``build_bakeoff_spec.py`` does for artifact kinds ``bakeoff_state.py`` has no dedicated
validator for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_state import (  # noqa: E402
    BakeoffContractError,
    sign_artifact,
    validate_bakeoff_spec,
    validate_corpus_manifest,
    validate_signed_artifact,
)
from build_evaluation_queries import load_manifest  # noqa: E402


class JudgingMatrixBuildError(ValueError):
    """The Gate-D JudgingMatrix cannot be assembled from the supplied frozen inputs."""


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def contender_id_for_cell(cell: Mapping[str, Any]) -> str:
    """Derive one contender ID from a ``RetrievalRunBundle``'s ``cell`` field.

    Mirrors exactly how ``build_bakeoff_spec.load_contenders`` constructed the spec's
    ``contenders`` strings in the first place (``contender_id = f"{candidate.kind}:{logical_id}:
    entity"``): ``"lexical:bm25"`` for the fixed lexical baseline (never derived from its
    ``channel``, which records the BM25-plus-current-head search strategy, not an identity axis),
    else ``f'{cell["kind"]}:{cell["model_id"]}:{cell["channel"]}'`` for ``dense``/
    ``late_interaction`` cells.
    """
    kind = cell.get("kind")
    if kind == "lexical":
        return "lexical:bm25"
    if kind in {"dense", "late_interaction"}:
        model_id = cell.get("model_id")
        channel = cell.get("channel")
        if not isinstance(model_id, str) or not model_id:
            raise JudgingMatrixBuildError(f"{kind} cell is missing a model_id")
        if not isinstance(channel, str) or not channel:
            raise JudgingMatrixBuildError(f"{kind} cell is missing a channel")
        return f"{kind}:{model_id}:{channel}"
    raise JudgingMatrixBuildError(f"unsupported RetrievalRunBundle cell kind: {kind!r}")


def load_frozen_entity_text(
    corpus_export: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    """Hash-verify the unsigned corpus export against the signed manifest and return raw text.

    Reuses exactly the verification pattern already implemented in ``run_retrieval_bakeoff.py``'s
    ``load_frozen_documents`` (title/body/source hash checks, eligible-set equality) but returns
    plain ``{entity_id: {"title", "body"}}`` text instead of ``IndexDocument`` chunks -- pooling
    only ever needs whole-entity title/body text, never the model-indexing chunk representation,
    so chunk hashes are deliberately not re-verified here.
    """
    rows = corpus_export.get("entities")
    if not isinstance(rows, list):
        raise JudgingMatrixBuildError("corpus export lacks entities")
    by_id = {row.get("entity_id"): row for row in rows if isinstance(row, dict)}
    eligible_ids = manifest["eligible_ids"]
    if set(by_id) != set(eligible_ids):
        raise JudgingMatrixBuildError("corpus export eligible set differs from signed manifest")
    manifest_rows = {row["entity_id"]: row for row in manifest["entities"]}
    result: dict[str, dict[str, str]] = {}
    for entity_id in eligible_ids:
        row = by_id[entity_id]
        signed = manifest_rows[entity_id]
        if row.get("source_hash") != signed["source_hash"]:
            raise JudgingMatrixBuildError(f"source hash mismatch for {entity_id}")
        title = str(row.get("title", ""))
        body = str(row.get("body", ""))
        if _text_hash(title) != signed["title_hash"]:
            raise JudgingMatrixBuildError(f"title hash mismatch for {entity_id}")
        if _text_hash(body) != signed["body_hash"]:
            raise JudgingMatrixBuildError(f"body hash mismatch for {entity_id}")
        result[entity_id] = {"title": title, "body": body}
    return result


def load_bundles(
    retrieval_runs_dir: Path,
    spec: Mapping[str, Any],
    *,
    expected_contenders: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load, validate, and identify every signed ``RetrievalRunBundle`` in ``retrieval_runs_dir``.

    Every bundle must: validate as a signed ``RetrievalRunBundle``, declare the exact
    ``spec_fingerprint`` of the supplied spec, carry zero ``failures``, and resolve to a unique
    contender ID.  The resulting contender-ID set must equal ``spec["contenders"]`` exactly (no
    missing, no extra) -- this is the hard integrity gate that proves every declared contender
    really executed and nothing untracked snuck in.
    """
    paths = sorted(retrieval_runs_dir.glob("*.json"))
    if not paths:
        raise JudgingMatrixBuildError(f"no retrieval run bundles found under {retrieval_runs_dir}")
    bundles: dict[str, dict[str, Any]] = {}
    for path in paths:
        artifact = json.loads(path.read_text(encoding="utf-8"))
        bundle = validate_signed_artifact(artifact, kind="RetrievalRunBundle")
        if bundle.get("spec_fingerprint") != spec["artifact_fingerprint"]:
            raise JudgingMatrixBuildError(
                f"{path.name}: spec_fingerprint does not match the supplied BakeoffSpec"
            )
        failures = bundle.get("failures")
        if failures:
            raise JudgingMatrixBuildError(f"{path.name}: bundle has non-empty failures")
        contender_id = contender_id_for_cell(bundle.get("cell") or {})
        if contender_id in bundles:
            raise JudgingMatrixBuildError(
                f"duplicate contender ID {contender_id!r} across retrieval run bundles "
                f"({path.name} and a previously loaded file)"
            )
        bundles[contender_id] = bundle
    expected = set(expected_contenders or spec["contenders"])
    actual = set(bundles)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing: {missing}")
        if extra:
            detail.append(f"extra: {extra}")
        raise JudgingMatrixBuildError(
            f"retrieval run bundle contender set does not match spec contenders ({'; '.join(detail)})"
        )
    return bundles


def build_pools(  # noqa: C901, PLR0912
    queries: Sequence[Mapping[str, Any]],
    bundles: Mapping[str, dict[str, Any]],
    entity_text: Mapping[str, dict[str, str]],
    *,
    pool_top_n: int = 20,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Build one contender-union top-N candidate pool per query, force-including source entities.

    Raises on any integrity gap: a pooled or force-included ``entity_id`` absent from
    ``entity_text`` (the corpus export is missing something a real retrieval run actually
    returned, or something a query genuinely cites -- a hard integrity failure, never silently
    skipped), a bundle missing a result row for a query, a bundle with duplicate ``query_id``s in
    its results, or a query that ends up with an empty pool.
    """
    if pool_top_n < 1:
        raise JudgingMatrixBuildError("pool_top_n must be a positive integer")

    results_by_contender: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for contender_id, bundle in bundles.items():
        rows = bundle.get("results")
        if not isinstance(rows, list):
            raise JudgingMatrixBuildError(f"{contender_id}: bundle has no results list")
        by_query: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            query_id = row.get("query_id")
            if not isinstance(query_id, str) or not query_id:
                raise JudgingMatrixBuildError(f"{contender_id}: result row has an invalid query_id")
            if query_id in by_query:
                raise JudgingMatrixBuildError(
                    f"{contender_id}: duplicate query_id {query_id!r} in bundle results"
                )
            top20 = row.get("top20")
            if not isinstance(top20, list):
                raise JudgingMatrixBuildError(
                    f"{contender_id}: result row {query_id!r} has no top20"
                )
            by_query[query_id] = top20
        results_by_contender[contender_id] = by_query

    pools: dict[str, dict[str, dict[str, Any]]] = {}
    for query in queries:
        query_id = query["id"]
        pool: dict[str, dict[str, Any]] = {}
        for contender_id, by_query in results_by_contender.items():
            if query_id not in by_query:
                raise JudgingMatrixBuildError(
                    f"{contender_id}: missing a result row for query {query_id!r}"
                )
            for hit in by_query[query_id][:pool_top_n]:
                if not isinstance(hit, dict):
                    raise JudgingMatrixBuildError(
                        f"{contender_id}: malformed top20 hit for query {query_id!r}"
                    )
                entity_id = hit.get("entity_id")
                if not isinstance(entity_id, str) or not entity_id:
                    raise JudgingMatrixBuildError(
                        f"{contender_id}: top20 hit for query {query_id!r} has no entity_id"
                    )
                if entity_id not in entity_text:
                    raise JudgingMatrixBuildError(
                        f"pooled entity_id {entity_id!r} (from {contender_id}, query {query_id!r}) "
                        "is not present in the hash-verified corpus export"
                    )
                if entity_id not in pool:
                    text = entity_text[entity_id]
                    pool[entity_id] = {
                        "title": text["title"],
                        "full_content": text["body"],
                        "ground_truth_forced_include": False,
                    }
        for source_entity_id in query.get("source_entity_ids", []) or []:
            if not isinstance(source_entity_id, str) or not source_entity_id:
                raise JudgingMatrixBuildError(f"query {query_id!r} has an invalid source_entity_id")
            if source_entity_id not in entity_text:
                raise JudgingMatrixBuildError(
                    f"source_entity_id {source_entity_id!r} (query {query_id!r}) is not present "
                    "in the hash-verified corpus export"
                )
            if source_entity_id not in pool:
                text = entity_text[source_entity_id]
                pool[source_entity_id] = {
                    "title": text["title"],
                    "full_content": text["body"],
                    "ground_truth_forced_include": True,
                }
        if not pool:
            raise JudgingMatrixBuildError(f"query {query_id!r} produced an empty candidate pool")
        pools[query_id] = pool
    return pools


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    os.replace(temporary, path)


def build_judging_matrix(
    *,
    spec_path: Path,
    retrieval_runs_dir: Path,
    corpus_manifest_path: Path,
    corpus_export_path: Path,
    queries_dev_path: Path,
    pool_top_n: int = 20,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble and sign one Gate-D ``JudgingMatrix`` from real frozen upstream artifacts."""
    del repo_root  # accepted for CLI/signature symmetry with sibling scripts; unused here
    spec = validate_bakeoff_spec(json.loads(spec_path.read_text(encoding="utf-8")))
    manifest = validate_corpus_manifest(
        json.loads(corpus_manifest_path.read_text(encoding="utf-8"))
    )
    corpus_export = json.loads(corpus_export_path.read_text(encoding="utf-8"))
    if not isinstance(corpus_export, dict):
        raise JudgingMatrixBuildError("corpus export must contain a JSON object")
    entity_text = load_frozen_entity_text(corpus_export, manifest)

    manifest_doc = load_manifest(queries_dev_path, expected_split="dev", require_provenance=True)
    queries = manifest_doc["queries"]
    if len(queries) != 400:
        raise JudgingMatrixBuildError(f"expected exactly 400 dev queries, found {len(queries)}")

    bundles = load_bundles(retrieval_runs_dir, spec)
    pools = build_pools(queries, bundles, entity_text, pool_top_n=pool_top_n)

    payload = {
        "spec_fingerprint": spec["artifact_fingerprint"],
        "corpus_root_hash": manifest["corpus_root_hash"],
        "contenders": sorted(bundles),
        "query_count": len(queries),
        "pool_top_n": pool_top_n,
        "pools": pools,
    }
    return sign_artifact("JudgingMatrix", payload)


def build_blind_judging_matrix(
    *,
    spec_path: Path,
    retrieval_runs_dir: Path,
    corpus_manifest_path: Path,
    corpus_export_path: Path,
    queries_blind_path: Path,
    vault_dir: Path,
    winner_path: Path,
    unlock_path: Path,
    manifest_receipt_path: Path,
) -> dict[str, Any]:
    """Build the 800-query matrix only after authorizing the sealed blind manifest.

    Unlike the development builder, only the signed development winner and lexical BM25 are
    admissible.  Query text remains in this short-lived function and never enters the output
    artifact (the matrix is keyed exclusively by opaque query IDs).
    """
    spec = validate_bakeoff_spec(json.loads(spec_path.read_text(encoding="utf-8")))
    winner = validate_signed_artifact(
        json.loads(winner_path.read_text(encoding="utf-8")), kind="DevelopmentWinner"
    )
    winner_id = winner.get("pipeline", {}).get("contender_id")
    expected = {winner_id, "lexical:bm25"}
    if (
        not isinstance(winner_id, str)
        or winner_id == "lexical:bm25"
        or not expected.issubset(set(spec["contenders"]))
    ):
        raise JudgingMatrixBuildError(
            "blind matrix requires one non-lexical signed winner and lexical:bm25"
        )
    # authorize_blind_file validates all control artifacts before opening this path.
    from bakeoff_state import authorize_blind_file

    query_bytes = authorize_blind_file(
        queries_blind_path, vault_dir, spec_path, winner_path, unlock_path, manifest_receipt_path
    )
    try:
        manifest_doc = json.loads(query_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JudgingMatrixBuildError("authorized blind manifest is not JSON") from exc
    queries = manifest_doc.get("queries") if isinstance(manifest_doc, dict) else None
    if not isinstance(queries, list) or len(queries) != 800:
        raise JudgingMatrixBuildError("authorized blind manifest must contain exactly 800 queries")
    query_ids = [row.get("id") for row in queries if isinstance(row, dict)]
    if (
        len(query_ids) != 800
        or len(set(query_ids)) != 800
        or any(not isinstance(x, str) or not x for x in query_ids)
    ):
        raise JudgingMatrixBuildError("authorized blind manifest has invalid query IDs")
    if any(row.get("split") != "blind" for row in queries):
        raise JudgingMatrixBuildError("blind matrix rejects non-blind query rows")
    manifest = validate_corpus_manifest(
        json.loads(corpus_manifest_path.read_text(encoding="utf-8"))
    )
    corpus_export = json.loads(corpus_export_path.read_text(encoding="utf-8"))
    entity_text = load_frozen_entity_text(corpus_export, manifest)
    bundles = load_bundles(retrieval_runs_dir, spec, expected_contenders=sorted(expected))
    pools = build_pools(queries, bundles, entity_text, pool_top_n=20)
    return sign_artifact(
        "JudgingMatrix",
        {
            "spec_fingerprint": spec["artifact_fingerprint"],
            "corpus_root_hash": manifest["corpus_root_hash"],
            "contenders": sorted(bundles),
            "query_count": 800,
            "pool_top_n": 20,
            "pools": pools,
        },
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="Signed Gate-D bakeoff_spec.json")
    parser.add_argument(
        "--retrieval-runs-dir",
        type=Path,
        required=True,
        help="Directory of signed RetrievalRunBundle JSON files (one per contender)",
    )
    parser.add_argument(
        "--corpus-manifest",
        type=Path,
        required=True,
        help="Signed corpus_representation_manifest.json",
    )
    parser.add_argument(
        "--corpus-export",
        type=Path,
        required=True,
        help="Unsigned corpus_export.json (title/body text)",
    )
    parser.add_argument(
        "--queries-dev",
        type=Path,
        required=True,
        help="Signed dev query manifest (queries_dev.json)",
    )
    parser.add_argument(
        "--pool-top-n", type=int, default=20, help="Per-contender pooling depth (default: 20)"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="Output path for the signed JudgingMatrix"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    matrix = build_judging_matrix(
        spec_path=args.spec,
        retrieval_runs_dir=args.retrieval_runs_dir,
        corpus_manifest_path=args.corpus_manifest,
        corpus_export_path=args.corpus_export,
        queries_dev_path=args.queries_dev,
        pool_top_n=args.pool_top_n,
    )
    _atomic_write(args.out, matrix)
    print(json.dumps(matrix, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BakeoffContractError, JudgingMatrixBuildError) as exc:
        raise SystemExit(str(exc)) from exc
