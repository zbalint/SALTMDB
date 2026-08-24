"""Generate the public-safe synthetic corpus used by the historical speed benchmark.

The generated records are intentionally artificial. They preserve a useful distribution of
text lengths and operational vocabulary without copying memories, transcripts, customer data,
or any other material from a live SALTMDB database.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TOPICS = (
    "cache invalidation",
    "document indexing",
    "queue processing",
    "API compatibility",
    "backup verification",
    "schema migration",
    "permission review",
    "incident response",
    "model evaluation",
    "release validation",
)

ACTIONS = (
    "compare the observed state with the declared invariant",
    "record the decision together with its falsifiable evidence",
    "run the smallest deterministic check before changing configuration",
    "separate the reversible experiment from the production-facing decision",
    "retain provenance so a later maintainer can reconstruct the reasoning",
)

CONSTRAINTS = (
    "The procedure must be repeatable without network access.",
    "Inputs are synthetic and contain no personal or organizational information.",
    "Failure must leave the previous state available for inspection.",
    "The result is accepted only after an independent consistency check.",
    "Generated identifiers are placeholders and carry no external meaning.",
)


def build_sample_texts(count: int = 200) -> list[str]:
    """Return deterministic synthetic records with varied chunking characteristics."""
    records: list[str] = []
    for index in range(count):
        topic = TOPICS[index % len(TOPICS)]
        action = ACTIONS[(index // len(TOPICS)) % len(ACTIONS)]
        constraint = CONSTRAINTS[(index * 3) % len(CONSTRAINTS)]
        detail_count = 2 + index % 9
        details = " ".join(
            f"Step {step + 1} for synthetic case {index:03d}: {action}; {constraint}"
            for step in range(detail_count)
        )
        records.append(
            f"[SYNTHETIC BENCHMARK RECORD {index:03d}]\n\n"
            f"Topic: {topic}. This record exists only to exercise deterministic text chunking "
            "and embedding throughput. It is not derived from a user session or memory store.\n\n"
            f"Procedure: {details}\n\n"
            f"Expected result: the {topic} example remains reproducible, auditable, and safe to "
            "publish as open-source benchmark input."
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("sample_texts.json"),
    )
    args = parser.parse_args()
    if args.count < 100:
        raise SystemExit("--count must be at least 100 for the benchmark's N=100 case")
    args.out.write_text(json.dumps(build_sample_texts(args.count), indent=2) + "\n")


if __name__ == "__main__":
    main()
