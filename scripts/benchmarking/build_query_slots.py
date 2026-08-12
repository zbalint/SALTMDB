"""Build Gate-B query source-slots and sealed ``QuerySlotAssignments`` for one accuracy bakeoff.

This is the snapshot-export-driven replacement for opening a live/throwaway SQLite corpus copy
with ``build_evaluation_queries.py --db-path``.  Its only inputs are the frozen, already-signed
Gate-A-adjacent corpus artifacts (``corpus_export.json`` + ``corpus_representation_manifest.json``
produced by ``freeze_live_corpus.py``) -- it never opens SALTMDB's live database, and it never
materializes blind query *text* (only sealed blind slot *metadata*, which carries no query text
or instruction, is safe to freeze ahead of the development winner per ``bakeoff_state``'s custody
rules).

Facet taxonomy
--------------
``bakeoff_state.MANDATORY_FACETS`` (the taxonomy a signed ``BakeoffSpec``/``QuerySlotAssignments``
require) is a distinct, newer, nine-facet scheme -- ``exact_sentence``, ``keyword``, ``typo``,
``short_memory``, ``long_body``, ``current_vs_superseded``, ``close_sibling``, ``multilingual``,
``strict_negative`` -- from the older thirteen-category scheme
(``build_evaluation_queries.EVALUATION_CATEGORY_TARGETS``) built for the prior, much larger
21,771-entity diverse corpus.  This module targets the new nine-facet scheme directly, using each
facet name as the slot ``category`` so the existing, already-tested ``validate_slots``/
``assign_slots``/``materialize_queries``/``write_manifest`` machinery in
``build_evaluation_queries.py`` applies completely unmodified -- only the facet-to-corpus mapping
below is new.

Live snapshot data gap
-----------------------
The frozen 479-entity live snapshot (``accuracy-bakeoff-20260812``) contains exactly one relation
predicate: ``supersedes`` (50 edges, all fully resolving to entities inside the export).  There are
zero ``elaborates_on``/``similar_to``/``resolves`` edges, so the old relation-driven
"closely_related_incident" construction has no data to draw on.  ``close_sibling`` is instead
derived from shared, moderately-rare significant title tokens across *different* entities --
computed entirely from ``corpus_export.json`` title text, with no dependency on any field outside
the signed manifest's hash coverage (title/body only).

Corpus binding
--------------
Every entity used here is cross-verified against ``corpus_representation_manifest.json``'s
per-entity ``title_hash``/``body_hash`` (recomputed with ``freeze_live_corpus.py``'s exact
normalization) before it may appear in a slot.  A single mismatch is a hard failure.  The returned
``corpus_root_hash`` is the manifest's own signed root and is the fingerprint every downstream
query manifest/assignment must declare.

Entity budget
-------------
Every source entity may back at most one facet (the shared slot/family-disjointness rules in
``build_evaluation_queries.validate_slots`` forbid the same entity id from ever appearing under two
different ``topic_family_id`` values).  With 479 entities and eight entity-consuming facets,
``FACET_TARGETS`` below deliberately keeps each entity-facet's quota modest (60 dev+blind slot-
units per facet, drawn from 30 entities at a uniform two-variant schedule) and lets the entity-free
``strict_negative`` facet absorb the remaining split volume (240 dev / 480 blind), which comfortably
fits inside the corpus without starving any other facet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_state import (  # noqa: E402
    MANDATORY_FACETS,
    sign_artifact,
    validate_corpus_manifest,
    validate_query_slot_assignments,
)
from build_evaluation_queries import (  # noqa: E402
    artifact_fingerprint,
    assign_slots,
    materialize_queries,
    validate_slots,
    write_manifest,
)
from query_generation import (  # noqa: E402
    generate_gibberish_query,
    generate_partial_word_nonsense_query,
    perturb_typo,
)


class GateBSlotError(ValueError):
    """The frozen export/manifest cannot satisfy a Gate-B facet quota or binding check."""


# Every mandatory facet must appear, with a positive count, summing exactly to the split total.
# See the module docstring's "Entity budget" section for why the entity-consuming facets are kept
# to 60 slot-units (20 dev / 40 blind) each while ``strict_negative`` absorbs the rest.
FACET_TARGETS: dict[str, dict[str, int]] = {
    "dev": {
        "exact_sentence": 20,
        "keyword": 20,
        "typo": 20,
        "short_memory": 20,
        "long_body": 20,
        "multilingual": 20,
        "current_vs_superseded": 20,
        "close_sibling": 20,
        "strict_negative": 240,
    },
    "blind": {
        "exact_sentence": 40,
        "keyword": 40,
        "typo": 40,
        "short_memory": 40,
        "long_body": 40,
        "multilingual": 40,
        "current_vs_superseded": 40,
        "close_sibling": 40,
        "strict_negative": 480,
    },
}
ENTITY_FACETS = tuple(sorted(MANDATORY_FACETS - {"strict_negative"}))
_SIMPLE_FACETS = ("exact_sentence", "keyword", "typo", "multilingual")  # short/long assigned separately

if set(FACET_TARGETS["dev"]) != MANDATORY_FACETS or set(FACET_TARGETS["blind"]) != MANDATORY_FACETS:
    raise GateBSlotError("FACET_TARGETS must declare exactly the mandatory facet set")
if sum(FACET_TARGETS["dev"].values()) != 400 or sum(FACET_TARGETS["blind"].values()) != 800:
    raise GateBSlotError("FACET_TARGETS must sum to the frozen 400/800 split")

FACET_INSTRUCTIONS: dict[str, str] = {
    "exact_sentence": "Return the supplied sentence verbatim as a search query with no changes.",
    "keyword": "Write a short keyword-style query (2-4 words) a user would type to find this "
    "memory, without forming a full sentence.",
    "typo": "Write a search query for this memory's title that contains one realistic typo or "
    "transposition.",
    "short_memory": "Write a short natural-language question about this memory.",
    "long_body": "Write a detailed natural-language question whose answer requires the memory's "
    "body text, referencing a specific distinguishing detail.",
    "current_vs_superseded": "Write a query asking for the CURRENT guidance on this topic, "
    "phrased so a superseded answer would be wrong.",
    "close_sibling": "Write a query that distinguishes this memory from a closely related sibling "
    "memory on a similar topic.",
    "multilingual": "Write a natural search query in the requested target language.",
    "strict_negative": "Write one negative-probe query (gibberish, off-topic, false-premise, or "
    "vocabulary-overlap-mismatch) with no correct answer in this corpus.",
}
if set(FACET_INSTRUCTIONS) != MANDATORY_FACETS:
    raise GateBSlotError("FACET_INSTRUCTIONS must cover exactly the mandatory facet set")

GENERATION_PROMPT_VERSION = "gate_b_facet_prompt_v1"
GENERATION_PROMPT = json.dumps(
    {"version": GENERATION_PROMPT_VERSION, "facets": FACET_INSTRUCTIONS}, sort_keys=True
)
GENERATION_PROMPT_HASH = hashlib.sha256(GENERATION_PROMPT.encode()).hexdigest()

_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "have",
        "were",
        "been",
        "which",
        "their",
        "about",
        "would",
        "could",
        "should",
        "there",
        "these",
        "those",
        "where",
        "when",
        "what",
        "while",
        "shall",
        "must",
        "only",
        "also",
        "into",
        "onto",
        "over",
        "under",
        "between",
        "because",
        "before",
        "after",
        "during",
        "every",
        "other",
        "another",
        "being",
        "still",
        "such",
        "than",
        "then",
        "them",
        "they",
        "will",
        "rule",
        "decision",
        "context",
        "rationale",
        "implementation",
        "policy",
        "guidance",
        "note",
        "notes",
        "fact",
        "facts",
    }
)
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z']{3,}")


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        raise GateBSlotError("entity title/body must be strings")
    return " ".join(value.split())


def load_export_bound_to_manifest(
    export_path: Path, manifest_path: Path
) -> tuple[dict[str, dict[str, str]], str]:
    """Load ``corpus_export.json`` entities, cross-verified against the signed manifest.

    Returns ``{entity_id: {"title": ..., "body": ...}}`` (normalized text only) plus the
    manifest's own ``corpus_root_hash``.  Every entity id must be present in both files with
    matching normalized-text hashes; any drift between the export and its signed manifest is a
    hard failure rather than a silent skip.
    """
    manifest = validate_corpus_manifest(json.loads(manifest_path.read_text()))
    export = json.loads(export_path.read_text())
    export_entities = export.get("entities")
    if not isinstance(export_entities, list) or not export_entities:
        raise GateBSlotError("corpus export entities must be a non-empty list")
    manifest_rows = {row["entity_id"]: row for row in manifest["entities"]}
    export_ids = {row["entity_id"] for row in export_entities}
    if export_ids != set(manifest["eligible_ids"]):
        raise GateBSlotError("corpus export entity IDs do not match the signed manifest")
    entities: dict[str, dict[str, str]] = {}
    for row in export_entities:
        entity_id = row["entity_id"]
        title = _normalize(row.get("title"))
        body = _normalize(row.get("body"))
        expected = manifest_rows[entity_id]
        if _sha256_text(title) != expected["title_hash"] or _sha256_text(body) != expected["body_hash"]:
            raise GateBSlotError(f"entity {entity_id} text does not match the signed manifest hash")
        entities[entity_id] = {"title": title, "body": body}
    return entities, manifest["corpus_root_hash"]


def find_sibling_pairs(
    entities: dict[str, dict[str, str]], excluded: set[str], needed: int
) -> list[tuple[str, str]]:
    """Deterministically pair entities sharing moderately rare significant title tokens.

    Tokens shared by exactly 2-4 titles are treated as a real topical signal (a token shared by
    every title would be generic boilerplate; a token unique to one title cannot pair anything).
    Pairs are ranked by shared-token count (ties broken by entity id) and greedily selected so no
    entity is reused across pairs -- each pair becomes one ``close_sibling`` topic family and both
    member entities are locked to it.
    """
    pool = sorted(entity_id for entity_id in entities if entity_id not in excluded)
    token_index: dict[str, list[str]] = {}
    for entity_id in pool:
        for token in sorted(set(_tokenize(entities[entity_id]["title"])) - _STOPWORDS):
            token_index.setdefault(token, []).append(entity_id)
    scores: dict[tuple[str, str], int] = {}
    for ids in token_index.values():
        if not 2 <= len(ids) <= 4:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair = (ids[i], ids[j]) if ids[i] < ids[j] else (ids[j], ids[i])
                scores[pair] = scores.get(pair, 0) + 1
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    used: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for (first, second), _count in ranked:
        if first in used or second in used:
            continue
        pairs.append((first, second))
        used.add(first)
        used.add(second)
        if len(pairs) == needed:
            break
    return pairs


def pick_supersedes_families(
    export: Mapping[str, Any], entities: dict[str, dict[str, str]], needed: int
) -> list[tuple[str, str]]:
    """Return up to ``needed`` distinct ``(current_id, superseded_id)`` pairs, both bound entities.

    Self-loop edges (``source_id == target_id``) carry no distinguishing content and are skipped;
    each current (target) entity is used at most once even if it has multiple superseded sources.
    """
    edges = export.get("supersedes_edges") or []
    seen_targets: set[str] = set()
    families: list[tuple[str, str]] = []
    for edge in sorted(edges, key=lambda item: (item["target_id"], item["source_id"])):
        target, source = edge["target_id"], edge["source_id"]
        if target == source or target in seen_targets:
            continue
        if target not in entities or source not in entities:
            continue
        seen_targets.add(target)
        families.append((target, source))
        if len(families) == needed:
            break
    return families


def _variant_schedule(num_families: int, total_target: int) -> list[int]:
    """One positive slot-variant count per family, summing exactly to ``total_target``."""
    if num_families <= 0:
        raise GateBSlotError("no families available for this facet")
    if total_target < num_families:
        raise GateBSlotError("facet target is smaller than its available family count")
    base, remainder = divmod(total_target, num_families)
    return [base + 1 if i < remainder else base for i in range(num_families)]


def _extract_sentence(body: str) -> str:
    parts = re.split(r"(?<=[.!?]) ", body)
    candidates = [part.strip() for part in parts if 30 <= len(part.strip()) <= 220]
    if candidates:
        return candidates[0]
    return body[:200].strip() or body


def _extract_keyword_phrase(body: str, title: str) -> str:
    title_tokens = set(_tokenize(title))
    tokens = [t for t in _tokenize(body) if t not in _STOPWORDS and t not in title_tokens]
    chosen = tokens[:3] or [t for t in _tokenize(body) if t not in _STOPWORDS][:3]
    return " ".join(chosen) if chosen else title


_SlotCounter = list  # a single-element list used as a mutable int box


def _new_slot(
    counter: _SlotCounter,
    *,
    category: str,
    subtype: str,
    source_entity_ids: list[str],
    family: str,
    instruction: str,
    source_text: str,
    language: str = "English",
) -> dict[str, Any]:
    counter[0] += 1
    number = counter[0]
    return {
        "slot_id": f"slot-{number:04d}",
        "query_id": f"eval-{number:04d}",
        "instruction": instruction,
        "source_text": source_text,
        "target_language": language,
        "lang": "en" if language == "English" else "und",
        "category": category,
        "subtype": subtype,
        "source_entity_ids": list(source_entity_ids),
        "topic_family_id": family,
        "provenance": "pending-generation",
    }


def build_source_slots_from_export(  # noqa: C901, PLR0912, PLR0915
    export_path: Path, manifest_path: Path
) -> tuple[list[dict[str, Any]], str]:
    """Build all 1200 Gate-B source slots bound to the frozen live snapshot.

    Returns ``(slots, corpus_root_hash)``.  ``slots`` are not yet split-assigned; pass them to
    ``build_evaluation_queries.assign_slots(slots, 400, 800, category_targets=FACET_TARGETS)``.
    """
    entities, corpus_root_hash = load_export_bound_to_manifest(export_path, manifest_path)
    export = json.loads(export_path.read_text())
    counter: _SlotCounter = [0]
    slots: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    total_cvs = FACET_TARGETS["dev"]["current_vs_superseded"] + FACET_TARGETS["blind"]["current_vs_superseded"]
    cvs_families = pick_supersedes_families(export, entities, needed=total_cvs // 2)
    if not cvs_families:
        raise GateBSlotError("frozen export has no usable supersedes pairs for current_vs_superseded")
    for (current_id, old_id), variants in zip(
        cvs_families, _variant_schedule(len(cvs_families), total_cvs)
    ):
        current, old = entities[current_id], entities[old_id]
        family = f"entity:{current_id}"
        for _ in range(variants):
            slots.append(
                _new_slot(
                    counter,
                    category="current_vs_superseded",
                    subtype="supersedes",
                    source_entity_ids=[current_id],
                    family=family,
                    instruction=FACET_INSTRUCTIONS["current_vs_superseded"],
                    source_text=f"Current title: {current['title']}\nSuperseded title: {old['title']}",
                )
            )
        used_ids.add(current_id)

    total_sibling = FACET_TARGETS["dev"]["close_sibling"] + FACET_TARGETS["blind"]["close_sibling"]
    sibling_pairs = find_sibling_pairs(entities, excluded=used_ids, needed=total_sibling // 2)
    if len(sibling_pairs) < total_sibling // 2:
        raise GateBSlotError(
            f"frozen export lacks enough independent close_sibling pairs: "
            f"found {len(sibling_pairs)}, need {total_sibling // 2}"
        )
    for (first_id, second_id), variants in zip(
        sibling_pairs, _variant_schedule(len(sibling_pairs), total_sibling)
    ):
        first, second = entities[first_id], entities[second_id]
        family = f"siblings:{first_id}:{second_id}"
        for _ in range(variants):
            slots.append(
                _new_slot(
                    counter,
                    category="close_sibling",
                    subtype="title_keyword_overlap",
                    source_entity_ids=[first_id, second_id],
                    family=family,
                    instruction=FACET_INSTRUCTIONS["close_sibling"],
                    source_text=f"Title A: {first['title']}\nTitle B: {second['title']}",
                )
            )
        used_ids.update((first_id, second_id))

    remaining = sorted(entity_id for entity_id in entities if entity_id not in used_ids)
    # Uniform two-variant schedule per simple facet: (dev+blind slot-units) // 2 distinct entities.
    per_facet_entities = max(
        1,
        (FACET_TARGETS["dev"]["exact_sentence"] + FACET_TARGETS["blind"]["exact_sentence"]) // 2,
    )
    needed_pool = per_facet_entities * (len(_SIMPLE_FACETS) + 2)  # +short_memory +long_body
    if len(remaining) < needed_pool:
        raise GateBSlotError(
            f"frozen export lacks enough unused entities for the simple facets: "
            f"found {len(remaining)}, need {needed_pool}"
        )
    by_body_length = sorted(remaining, key=lambda entity_id: (len(entities[entity_id]["body"]), entity_id))
    short_ids = by_body_length[:per_facet_entities]
    long_ids = by_body_length[-per_facet_entities:]
    consumed = set(short_ids) | set(long_ids)
    rest = sorted(entity_id for entity_id in remaining if entity_id not in consumed)

    def _simple_family_slots(
        facet: str, entity_ids: list[str], make_source_text
    ) -> None:
        total = FACET_TARGETS["dev"][facet] + FACET_TARGETS["blind"][facet]
        for entity_id, variants in zip(entity_ids, _variant_schedule(len(entity_ids), total)):
            entity = entities[entity_id]
            family = f"entity:{entity_id}"
            language = "French" if facet == "multilingual" else "English"
            for _ in range(variants):
                slots.append(
                    _new_slot(
                        counter,
                        category=facet,
                        subtype="fact",
                        source_entity_ids=[entity_id],
                        family=family,
                        instruction=FACET_INSTRUCTIONS[facet],
                        source_text=make_source_text(entity),
                        language=language,
                    )
                )
            used_ids.add(entity_id)

    _simple_family_slots(
        "short_memory", short_ids, lambda entity: entity["title"]
    )
    _simple_family_slots(
        "long_body",
        long_ids,
        lambda entity: f"Title: {entity['title']}\n\n{entity['body'][:6000]}",
    )
    facet_pools = {
        "exact_sentence": rest[0:per_facet_entities],
        "keyword": rest[per_facet_entities : 2 * per_facet_entities],
        "typo": rest[2 * per_facet_entities : 3 * per_facet_entities],
        "multilingual": rest[3 * per_facet_entities : 4 * per_facet_entities],
    }
    _simple_family_slots(
        "exact_sentence",
        facet_pools["exact_sentence"],
        lambda entity: _extract_sentence(entity["body"]) or entity["title"],
    )
    _simple_family_slots(
        "keyword",
        facet_pools["keyword"],
        lambda entity: f"Title: {entity['title']}\n\n{entity['body'][:6000]}",
    )
    _simple_family_slots("typo", facet_pools["typo"], lambda entity: entity["title"])
    _simple_family_slots(
        "multilingual", facet_pools["multilingual"], lambda entity: entity["title"]
    )

    total_negative = FACET_TARGETS["dev"]["strict_negative"] + FACET_TARGETS["blind"]["strict_negative"]
    negative_subtypes = (
        "pure_gibberish",
        "partial_real_word_nonsense",
        "nl_off_topic",
        "false_premise",
        "fictional_unanswerable",
        "vocabulary_overlap_mismatch",
    )
    for index in range(total_negative):
        number = counter[0] + 1
        counter[0] = number
        subtype = negative_subtypes[index % len(negative_subtypes)]
        slots.append(
            {
                "slot_id": f"slot-{number:04d}",
                "query_id": f"eval-{number:04d}",
                "instruction": FACET_INSTRUCTIONS["strict_negative"],
                "source_text": "No corpus content is required for this negative probe.",
                "target_language": "English",
                "lang": "en",
                "category": "strict_negative",
                "subtype": subtype,
                "source_entity_ids": [],
                "topic_family_id": f"negative:{number:04d}",
                "provenance": "pending-generation",
            }
        )

    validate_slots(slots)
    if len(slots) != 1200:
        raise GateBSlotError(f"expected exactly 1200 source slots, built {len(slots)}")
    return slots, corpus_root_hash


def deterministic_local_generation(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:  # noqa: C901
    """Auditable, LLM-free fallback query text for the nine Gate-B facets.

    Mirrors ``build_evaluation_queries.deterministic_local_generation``'s
    ``local:deterministic-fallback`` provenance convention: never mistaken for an independent
    external paraphrase in the final decision record.
    """
    results = []
    seen: set[str] = set()
    for index, slot in enumerate(slots):
        facet = slot["category"]
        text = slot["source_text"]
        if facet == "exact_sentence":
            query = text
        elif facet == "keyword":
            title, _, body = text.partition("\n\n")
            title = title.removeprefix("Title: ").strip()
            query = _extract_keyword_phrase(body, title)
        elif facet == "typo":
            query = perturb_typo(text, seed=index)
        elif facet in {"short_memory", "multilingual"}:
            query = f"What is {text}?" if facet == "short_memory" else text
        elif facet == "long_body":
            title, _, body = text.partition("\n\n")
            title = title.removeprefix("Title: ").strip()
            phrase = _extract_keyword_phrase(body, title)
            query = f"What is the documented guidance about {title}, specifically regarding {phrase}?"
        elif facet == "current_vs_superseded":
            current_line = next(
                line for line in text.splitlines() if line.startswith("Current title: ")
            )
            title = current_line.removeprefix("Current title: ").strip()
            query = f"What is the current guidance for {title}?"
        elif facet == "close_sibling":
            first_line, second_line = text.splitlines()
            title_a = first_line.removeprefix("Title A: ").strip()
            title_b = second_line.removeprefix("Title B: ").strip()
            query = f"What specifically distinguishes {title_a} from {title_b}?"
        elif facet == "strict_negative":
            templates = {
                "pure_gibberish": lambda: generate_gibberish_query(seed=index),
                "partial_real_word_nonsense": lambda: generate_partial_word_nonsense_query(seed=index),
                "nl_off_topic": lambda: "How do I train a pet dragon to repair a bicycle?",
                "false_premise": lambda: "Which documented policy requires every memory to be deleted nightly?",
                "fictional_unanswerable": lambda: "What was the color of the captain's invisible spacecraft?",
                "vocabulary_overlap_mismatch": lambda: "How does a cache invalidate a cooking recipe's flavor?",
            }
            query = templates[slot["subtype"]]()
        else:  # pragma: no cover - FACET_TARGETS/MANDATORY_FACETS keep this unreachable
            raise GateBSlotError(f"unknown facet {facet!r}")
        if query.casefold() in seen:
            query = f"{query} [variant-{index + 1}]"
        seen.add(query.casefold())
        results.append({"slot_id": slot["slot_id"], "query": query, "lang": slot["lang"]})
    return results


def build_query_slot_assignments(assigned_slots: list[dict[str, Any]]) -> dict[str, Any]:
    """Seal dev/blind slot metadata (no query text) as a signed ``QuerySlotAssignments`` artifact."""
    if len(assigned_slots) != 1200:
        raise GateBSlotError("query-slot assignments require exactly 1200 assigned slots")
    rows = [
        {
            "query_id": slot["query_id"],
            "slot_id": slot["slot_id"],
            "split": slot["split"],
            "topic_family_id": slot["topic_family_id"],
            "source_entity_ids": slot["source_entity_ids"],
            "facet": slot["category"],
        }
        for slot in assigned_slots
    ]
    artifact = sign_artifact(
        "QuerySlotAssignments",
        {"assignments": rows, "generation_prompt_hash": GENERATION_PROMPT_HASH},
    )
    return validate_query_slot_assignments(artifact)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True, help="Frozen corpus_export.json")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="Signed corpus_representation_manifest.json"
    )
    parser.add_argument("--slots-out", type=Path, required=True, help="Private slots JSON (never send externally)")
    parser.add_argument("--assignments-out", type=Path, required=True, help="Signed QuerySlotAssignments JSON")
    parser.add_argument("--dev-out", type=Path, help="Materialize the 400 dev queries with deterministic-fallback text")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    slots, corpus_root_hash = build_source_slots_from_export(args.export, args.manifest)
    assigned = assign_slots(slots, 400, 800, category_targets=FACET_TARGETS)
    args.slots_out.parent.mkdir(parents=True, exist_ok=True)
    args.slots_out.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "slots": assigned,
                "corpus_root_hash": corpus_root_hash,
                "fingerprint": artifact_fingerprint(assigned),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    assignments = build_query_slot_assignments(assigned)
    args.assignments_out.parent.mkdir(parents=True, exist_ok=True)
    args.assignments_out.write_text(json.dumps(assignments, indent=2, ensure_ascii=False))
    if args.dev_out:
        dev_slots = [slot for slot in assigned if slot["split"] == "dev"]
        generated = deterministic_local_generation(dev_slots)
        queries = materialize_queries(dev_slots, generated)
        write_manifest(
            queries,
            args.dev_out,
            corpus_fingerprint=corpus_root_hash,
            slot_fingerprint=artifact_fingerprint(dev_slots),
            targets={"dev": 400, "blind": 0},
            required_categories=set(MANDATORY_FACETS),
        )


if __name__ == "__main__":
    main()
