"""Freeze validated evaluation query manifests from local generation slots/results.

This module intentionally does not call an LLM.  It writes bounded request batches and accepts
only text/language keyed by an already-local slot, so source IDs never come from external output.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from query_generation import (
    QueryRow,
    classify_length_bucket,
    generate_gibberish_query,
    generate_partial_word_nonsense_query,
    perturb_typo,
)
from evaluation_artifacts import build_provenance  # noqa: E402


CANONICAL_SLOT_KEYS = frozenset(
    {
        "slot_id",
        "query_id",
        "instruction",
        "source_text",
        "target_language",
        "lang",
        "category",
        "subtype",
        "source_entity_ids",
        "topic_family_id",
        "provenance",
    }
)
QUERY_KEYS = frozenset(
    {
        "id",
        "query",
        "lang",
        "category",
        "subtype",
        "split",
        "source_entity_ids",
        "topic_family_id",
        "length_bucket",
        "provenance",
    }
)
NEGATIVE_CATEGORIES = (
    "pure_gibberish",
    "partial_real_word_nonsense",
    "nl_off_topic",
    "false_premise",
    "fictional_unanswerable",
    "vocabulary_overlap_mismatch",
)
POSITIVE_CATEGORIES = (
    "exact_title",
    "paraphrase",
    "body_text",
    "short_natural_language",
    "long_natural_language",
    "multilingual",
    "typo_partial",
)

# The pre-registered corpus quotas are expressed per split.  Short/long/typo are
# generation variants (kept in ``subtype`` where needed), not independent quota
# rows; the seven categories below are the complete scoring strata.
EVALUATION_CATEGORY_TARGETS = {
    "dev": {
        "exact_title": 60,
        "paraphrase": 60,
        "durable_decision": 40,
        "current_vs_superseded": 25,
        "closely_related_incident": 25,
        "multilingual": 60,
        "body_text": 30,
        "pure_gibberish": 20,
        "partial_real_word_nonsense": 20,
        "nl_off_topic": 20,
        "false_premise": 20,
        "fictional_unanswerable": 10,
        "vocabulary_overlap_mismatch": 10,
    },
    "blind": {
        "exact_title": 120,
        "paraphrase": 120,
        "durable_decision": 80,
        "current_vs_superseded": 50,
        "closely_related_incident": 50,
        "multilingual": 120,
        "body_text": 60,
        "pure_gibberish": 40,
        "partial_real_word_nonsense": 40,
        "nl_off_topic": 40,
        "false_premise": 40,
        "fictional_unanswerable": 20,
        "vocabulary_overlap_mismatch": 20,
    },
}


def artifact_fingerprint(value: object) -> str:
    """Stable content fingerprint for every cross-stage JSON artifact."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_artifact_fingerprint(value: object, *, field: str = "fingerprint") -> None:
    """Verify a signed JSON artifact before allowing it into another pipeline stage."""
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ValueError(f"artifact lacks {field}")
    unsigned = dict(value)
    stored = unsigned.pop(field)
    if stored != artifact_fingerprint(unsigned):
        raise ValueError(f"artifact {field} mismatch")


def validate_slots(slots: list[dict]) -> None:  # noqa: C901
    """Reject ambiguous slot input before it can be disclosed to a generator.

    Slots are intentionally strict: unknown fields are a common way that local provenance or
    entity bookkeeping leaks into an external generation batch by accident.
    """
    seen_slots, seen_queries = set(), set()
    source_families: dict[str, str] = {}
    family_splits: dict[str, str] = {}
    for slot in slots:
        if set(slot) not in (CANONICAL_SLOT_KEYS, CANONICAL_SLOT_KEYS | {"split"}):
            raise ValueError("slot does not have the canonical key set")
        for key in (
            "slot_id",
            "query_id",
            "instruction",
            "target_language",
            "lang",
            "category",
            "subtype",
            "topic_family_id",
            "provenance",
        ):
            if not isinstance(slot[key], str) or not slot[key].strip():
                raise ValueError(f"slot {key} must be a non-empty string")
        if not isinstance(slot["source_text"], str) or (
            not slot["source_text"].strip() and slot["category"] not in NEGATIVE_CATEGORIES
        ):
            raise ValueError("only negative slots may have empty source_text")
        if not isinstance(slot["source_entity_ids"], list) or not all(
            isinstance(value, str) and value for value in slot["source_entity_ids"]
        ):
            raise ValueError("slot source_entity_ids must be a list of non-empty strings")
        if slot["slot_id"] in seen_slots or slot["query_id"] in seen_queries:
            raise ValueError("duplicate slot_id or query_id")
        if "split" in slot and slot["split"] not in {"dev", "blind"}:
            raise ValueError("assigned slot split must be dev or blind")
        family = slot["topic_family_id"]
        if "split" in slot:
            previous_split = family_splits.setdefault(family, slot["split"])
            if previous_split != slot["split"]:
                raise ValueError("topic family occurs in both dev and blind slots")
        for source_id in slot["source_entity_ids"]:
            previous_family = source_families.setdefault(source_id, family)
            if previous_family != family:
                raise ValueError("source entity is assigned to multiple topic families")
        seen_slots.add(slot["slot_id"])
        seen_queries.add(slot["query_id"])


def validate_queries(
    queries: list[dict],
    *,
    targets: dict[str, int] | None = None,
    required_categories: set[str] | None = None,
) -> None:
    """Validate frozen manifests, including the split isolation rule.

    `targets`, when supplied, must be exact (the real experiment passes dev=400/blind=800).
    """
    families: dict[str, str] = {}
    texts, ids, categories = set(), set(), set()
    counts = {"dev": 0, "blind": 0}
    for query in queries:
        if set(query) != QUERY_KEYS:
            raise ValueError("query does not have the canonical key set")
        if query["split"] not in counts:
            raise ValueError("query split must be dev or blind")
        text = query["query"].strip().casefold()
        if not text or text in texts or query["id"] in ids:
            raise ValueError("empty or duplicate query text/id")
        texts.add(text)
        ids.add(query["id"])
        family = query["topic_family_id"]
        previous = families.setdefault(family, query["split"])
        if previous != query["split"]:
            raise ValueError("topic family occurs in both dev and blind")
        counts[query["split"]] += 1
        categories.add(query["category"])
    if targets and counts != targets:
        raise ValueError(f"split totals {counts} do not match required {targets}")
    missing = (required_categories or set()) - categories
    if missing:
        raise ValueError(f"required categories missing: {sorted(missing)}")


def _exact_family_subset(families: list[tuple[str, int]], target: int) -> set[str]:
    """Return a deterministic family subset whose slot count equals ``target``."""
    reachable: dict[int, tuple[int, str | None]] = {0: (-1, None)}
    for family, count in families:
        for total in sorted(tuple(reachable), reverse=True):
            candidate = total + count
            if candidate > target or candidate in reachable:
                continue
            reachable[candidate] = (total, family)
    if target not in reachable:
        raise ValueError("family sizes cannot satisfy exact category quota")
    selected: set[str] = set()
    total = target
    while total:
        previous, family = reachable[total]
        if family is None:
            raise ValueError("invalid family subset reconstruction")
        selected.add(family)
        total = previous
    return selected


def assign_slots(  # noqa: C901, PLR0912
    slots: list[dict],
    dev_target: int,
    blind_target: int,
    seed: int = 0,
    category_targets: dict[str, dict[str, int]] | None = None,
) -> list[dict]:
    validate_slots(slots)
    families: dict[str, list[dict]] = {}
    for slot in slots:
        families.setdefault(slot["topic_family_id"], []).append(slot)
    if category_targets is None and (dev_target, blind_target) == (400, 800):
        category_targets = EVALUATION_CATEGORY_TARGETS
    if category_targets is not None:
        expected_splits = {"dev", "blind"}
        if set(category_targets) != expected_splits:
            raise ValueError("category targets must define exactly dev and blind")
        if (
            sum(category_targets["dev"].values()) != dev_target
            or sum(category_targets["blind"].values()) != blind_target
        ):
            raise ValueError("category quotas do not match split totals")
        family_categories = {
            family: {member["category"] for member in members}
            for family, members in families.items()
        }
        if any(len(categories) != 1 for categories in family_categories.values()):
            raise ValueError("exact category quotas require one category per topic family")
        assigned: dict[str, str] = {}
        for category in sorted(category_targets["dev"]):
            candidates = sorted(
                (
                    (family, len(families[family]))
                    for family, categories in family_categories.items()
                    if categories == {category}
                ),
                key=lambda item: item[0],
            )
            selected = _exact_family_subset(candidates, category_targets["dev"][category])
            for family, _ in candidates:
                assigned[family] = "dev" if family in selected else "blind"
        if set(assigned) != set(families):
            raise ValueError("slots contain a category absent from the quota declaration")
        counts = {split: {} for split in ("dev", "blind")}
        for family, split in assigned.items():
            category = next(iter(family_categories[family]))
            counts[split][category] = counts[split].get(category, 0) + len(families[family])
        if counts != category_targets:
            raise ValueError(f"assigned category counts {counts} do not match quotas")
        return [{**slot, "split": assigned[slot["topic_family_id"]]} for slot in slots]

    # Without a declared quota, retain the generic family-safe cardinality split used by
    # small harness fixtures and exploratory callers.
    assigned: dict[str, str] = {}
    used = {"dev": 0, "blind": 0}
    for category in sorted({slot["category"] for slot in slots}):
        candidates = [
            family
            for family, members in sorted(families.items())
            if any(member["category"] == category for member in members)
        ]
        for split, target in (("dev", dev_target), ("blind", blind_target)):
            candidate = next(
                (
                    family
                    for family in candidates
                    if family not in assigned and used[split] + len(families[family]) <= target
                ),
                None,
            )
            if candidate is None:
                raise ValueError(
                    f"cannot reserve category {category!r} for {split} without splitting a family"
                )
            assigned[candidate] = split
            used[split] += len(families[candidate])
    for family, members in sorted(families.items(), key=lambda item: (-len(item[1]), item[0])):
        if family in assigned:
            continue
        count = len(members)
        dev_remaining, blind_remaining = dev_target - used["dev"], blind_target - used["blind"]
        preferred = "dev" if dev_remaining >= blind_remaining else "blind"
        alternate = "blind" if preferred == "dev" else "dev"
        if count <= (dev_remaining if preferred == "dev" else blind_remaining):
            split = preferred
        elif count <= (dev_remaining if alternate == "dev" else blind_remaining):
            split = alternate
        else:
            raise ValueError("family sizes cannot satisfy exact dev/blind targets")
        assigned[family] = split
        used[split] += count
    if used != {"dev": dev_target, "blind": blind_target}:
        raise ValueError(f"family split totals {used} do not match requested targets")
    return [{**slot, "split": assigned[slot["topic_family_id"]]} for slot in slots]


def build_batches(slots: list[dict], batch_size: int = 60) -> list[list[dict]]:
    if batch_size <= 0 or batch_size > 60:
        raise ValueError("batch_size must be 1..60")
    validate_slots(slots)
    # Never disclose local source IDs or family/split bookkeeping to a generator.
    safe = [
        {k: s[k] for k in ("slot_id", "instruction", "source_text", "target_language")}
        for s in slots
    ]
    return [safe[i : i + batch_size] for i in range(0, len(safe), batch_size)]


def materialize_queries(slots: list[dict], generated: list[dict]) -> list[dict]:
    validate_slots(slots)
    if any("split" not in slot for slot in slots):
        raise ValueError("slots must be assigned to dev or blind before materialization")
    generated_ids = [item.get("slot_id") for item in generated]
    if len(generated_ids) != len(set(generated_ids)):
        raise ValueError("generation output contains duplicate slot IDs")
    by_slot = {item["slot_id"]: item for item in generated}
    if set(by_slot) != {slot["slot_id"] for slot in slots}:
        raise ValueError("generation output must contain exactly one result for every slot")
    queries = []
    seen_text: set[str] = set()
    for slot in slots:
        item = by_slot[slot["slot_id"]]
        text = item.get("query", "").strip()
        if not text or text.casefold() in seen_text:
            raise ValueError("empty or duplicate generated query")
        seen_text.add(text.casefold())
        queries.append(
            QueryRow(
                id=slot["query_id"],
                query=text,
                lang=item.get("lang", slot.get("lang", "und")),
                category=slot["category"],
                subtype=slot["subtype"],
                split=slot["split"],
                source_entity_ids=slot.get("source_entity_ids", []),
                topic_family_id=slot["topic_family_id"],
                length_bucket=classify_length_bucket(text),
                provenance=item.get("provenance", slot["provenance"]),
            ).to_dict()
        )
    validate_queries(queries)
    return queries


def write_manifest(
    queries: list[dict],
    path: Path,
    *,
    corpus_fingerprint: str | None = None,
    slot_fingerprint: str | None = None,
    targets: dict[str, int] | None = None,
    required_categories: set[str] | None = None,
    commit_fingerprint: str | None = None,
    random_seed: int | None = None,
    config_fingerprint: str | None = None,
    judge_version_fingerprint: str | None = None,
    machine_fingerprint: str | None = None,
) -> dict:
    validate_queries(queries, targets=targets, required_categories=required_categories)
    result = {
        "schema_version": 1,
        "queries": queries,
        "queries_fingerprint": artifact_fingerprint(queries),
        "corpus_fingerprint": corpus_fingerprint,
        "slot_fingerprint": slot_fingerprint,
    }
    provenance_fields = (
        commit_fingerprint,
        corpus_fingerprint,
        random_seed,
        config_fingerprint,
        judge_version_fingerprint,
    )
    if all(value is not None for value in provenance_fields):
        result["provenance"] = build_provenance(
            commit_fingerprint=commit_fingerprint,
            corpus_fingerprint=corpus_fingerprint,
            query_manifest_fingerprint=result["queries_fingerprint"],
            random_seed=random_seed,
            config_fingerprint=config_fingerprint,
            judge_version_fingerprint=judge_version_fingerprint,
            machine_fingerprint_value=machine_fingerprint,
            artifact_kind="query_manifest",
        )
    else:
        # Existing exploratory manifests remain readable, but are explicitly marked unbound so a
        # promotion-grade caller can reject them instead of silently treating missing provenance
        # as compatible with the current run.
        result["provenance"] = {"status": "legacy_unbound", "stale": True}
    result["manifest_fingerprint"] = artifact_fingerprint(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    temporary.replace(path)
    return result


def load_manifest(
    path: Path,
    *,
    expected_split: str | None = None,
    expected_corpus_fingerprint: str | None = None,
    expected_slot_fingerprint: str | None = None,
    require_provenance: bool = False,
) -> dict:
    """Load and verify a frozen query manifest before matrix or judging execution."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("queries"), list):
        raise ValueError("query manifest must contain a queries list")
    verify_artifact_fingerprint(value, field="manifest_fingerprint")
    if value.get("queries_fingerprint") != artifact_fingerprint(value["queries"]):
        raise ValueError("query manifest queries_fingerprint mismatch")
    if (
        expected_corpus_fingerprint is not None
        and value.get("corpus_fingerprint") != expected_corpus_fingerprint
    ):
        raise ValueError("query manifest corpus fingerprint mismatch")
    if (
        expected_slot_fingerprint is not None
        and value.get("slot_fingerprint") != expected_slot_fingerprint
    ):
        raise ValueError("query manifest slot fingerprint mismatch")
    if require_provenance:
        provenance = value.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("stale"):
            raise ValueError("query manifest provenance is missing or stale")
    queries = value["queries"]
    validate_queries(queries)
    if expected_split is not None and any(item.get("split") != expected_split for item in queries):
        raise ValueError("query manifest contains a different split")
    return value


def build_source_slots_from_corpus(  # noqa: C901, PLR0912, PLR0915
    db_path: Path, *, positive_total: int = 900, negative_total: int = 300
) -> list[dict]:
    """Select deterministic, family-safe source slots from a frozen *copy* of the corpus.

    This deliberately makes no query text.  The returned private slots retain local entity IDs;
    `build_batches` is the only path that strips those before an external generator sees them.
    One source entity is used at most once, so singleton families cannot leak across the split.
    Relation endpoints are reserved for the two relation-focused categories and use a shared
    cluster family, preventing their partner from later entering the other split as a singleton.
    """
    if positive_total < 1 or negative_total < len(NEGATIVE_CATEGORIES):
        raise ValueError(
            "positive_total must be positive and negative_total must cover every negative category"
        )
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT id, title, full_content, memory_type FROM entities "
            "WHERE status != 'archived' ORDER BY id"
        ).fetchall()
        relations = connection.execute(
            "SELECT source_id, target_id, predicate FROM relations "
            "WHERE valid_to IS NULL ORDER BY source_id, target_id"
        ).fetchall()
    finally:
        connection.close()
    by_id = {row[0]: row for row in rows}
    relation_rows = [
        row
        for row in relations
        if row[2] in {"supersedes", "elaborates_on", "similar_to", "resolves"}
        and row[0] in by_id
        and row[1] in by_id
    ]
    reserved = {entity_id for source, target, _ in relation_rows for entity_id in (source, target)}
    ordinary = [row for row in rows if row[0] not in reserved]
    if len(ordinary) + len(relation_rows) < positive_total:
        raise ValueError(
            "frozen corpus lacks enough distinct source families for requested positive slots"
        )
    slots: list[dict] = []

    def add_slot(
        *,
        category: str,
        subtype: str,
        source_id: str,
        title: str,
        content: str,
        family: str,
        instruction: str,
        language: str = "English",
    ) -> None:
        number = len(slots) + 1
        slots.append(
            {
                "slot_id": f"slot-{number:04d}",
                "query_id": f"eval-{number:04d}",
                "instruction": instruction,
                "source_text": f"Title: {title}\n\n{content[:6000]}",
                "target_language": language,
                "lang": "en" if language == "English" else "und",
                "category": category,
                "subtype": subtype,
                "source_entity_ids": [source_id],
                "topic_family_id": family,
                "provenance": "pending-generation",
            }
        )

    if (positive_total, negative_total) == (900, 300):
        # The full run has fixed category quotas.  Build each category as whole source families
        # first, then assign those families with the exact quota-aware splitter below.  Relation
        # rows are repeated only as deterministic query-variant slots (never as new sources).
        current_rows = [
            (source, target, predicate)
            for source, target, predicate in relation_rows
            if predicate == "supersedes"
            and source != target
            and by_id[source][3] in {"fact", "event", "decision"}
        ]
        related_rows = [
            (source, target, predicate)
            for source, target, predicate in relation_rows
            if predicate != "supersedes" and by_id[source][3] == "fact"
        ]
        if not current_rows or not related_rows:
            raise ValueError("frozen corpus lacks relation rows for registered quotas")

        def add_relation_variants(
            rows_for_category, category: str, target_count: int, *, choose_target: bool = False
        ) -> set[str]:
            source_ids: set[str] = set()
            unique_rows = []
            seen_ids: set[str] = set()
            for source, target, predicate in rows_for_category:
                chosen = target if choose_target else source
                if chosen in seen_ids:
                    continue
                seen_ids.add(chosen)
                unique_rows.append((source, target, predicate, chosen))
            if len(unique_rows) < 30 and category == "current_vs_superseded":
                raise ValueError("frozen corpus lacks 30 independent current-guidance families")
            if category == "current_vs_superseded":
                unique_rows = unique_rows[:30]
                family_counts = [3] * 15 + [2] * 15
                row_schedule = [
                    row for row, count in zip(unique_rows, family_counts) for _ in range(count)
                ]
            else:
                row_schedule = unique_rows[:target_count]
            if len(row_schedule) < target_count:
                raise ValueError("frozen corpus lacks enough related-incident families")
            for source, target, predicate, chosen in row_schedule[:target_count]:
                row = by_id[chosen]
                source_ids.add(chosen)
                add_slot(
                    category=category,
                    subtype=predicate,
                    source_id=chosen,
                    title=row[1],
                    content=row[2],
                    family=f"entity:{chosen}",
                    instruction="Write a precise natural-language search query that distinguishes this memory from closely related context.",
                )
            return source_ids

        relation_source_ids = add_relation_variants(current_rows, "current_vs_superseded", 75)
        related_rows = [row for row in related_rows if row[0] not in relation_source_ids]
        if not related_rows:
            raise ValueError("frozen corpus lacks independent related-incident families")
        relation_source_ids |= add_relation_variants(related_rows, "closely_related_incident", 75)

        durable_rows = [
            row
            for row in rows
            if row[3] in {"decision", "event", "procedure"} and row[0] not in relation_source_ids
        ]
        if len(durable_rows) < 60:
            raise ValueError("frozen corpus lacks 60 durable source families")
        for row_index, row in enumerate(durable_rows[:60]):
            variants = 3 if row_index < 30 else 1
            for _ in range(variants):
                entity_id, title, content, memory_type = row
                add_slot(
                    category="durable_decision",
                    subtype=memory_type or "fact",
                    source_id=entity_id,
                    title=title,
                    content=content,
                    family=f"entity:{entity_id}",
                    instruction="Write a precise query about the documented decision, procedure, or fix.",
                )

        ordinary_full = [
            row
            for row in rows
            if row[0] not in relation_source_ids
            and row[0] not in {item[0] for item in durable_rows[:60]}
        ]
        category_plan = (
            ("exact_title", 180, "Write a concise exact-fact lookup query.", "English"),
            (
                "paraphrase",
                180,
                "Write a paraphrased conceptual search query; do not copy the title.",
                "English",
            ),
            (
                "multilingual",
                180,
                "Write a natural search query in the requested target language.",
                "French",
            ),
            (
                "body_text",
                90,
                "Write a query whose answer is supported by the body rather than title words.",
                "English",
            ),
        )
        cursor = 0
        for category, count, instruction, language in category_plan:
            for row in ordinary_full[cursor : cursor + count]:
                entity_id, title, content, memory_type = row
                add_slot(
                    category=category,
                    subtype=memory_type or "fact",
                    source_id=entity_id,
                    title=title,
                    content=content,
                    family=f"entity:{entity_id}",
                    instruction=instruction,
                    language=language,
                )
            cursor += count
        if cursor != 630 or len(slots) != 900:
            raise ValueError("could not satisfy full positive category quotas")
    else:
        # Small fixtures and exploratory callers retain the historical compact selector.
        used = set()
        for source, target, predicate in relation_rows:
            if len(slots) >= min(positive_total, 80):
                break
            chosen = target if predicate == "supersedes" else source
            if chosen in used:
                continue
            row = by_id[chosen]
            category = (
                "current_vs_superseded" if predicate == "supersedes" else "closely_related_incident"
            )
            add_slot(
                category=category,
                subtype=predicate,
                source_id=chosen,
                title=row[1],
                content=row[2],
                family=f"cluster:{target}",
                instruction="Write a precise natural-language search query that distinguishes this memory from closely related context.",
            )
            used.add(chosen)
        for row in ordinary:
            if len(slots) >= positive_total:
                break
            entity_id, title, content, memory_type = row
            category = POSITIVE_CATEGORIES[len(slots) % len(POSITIVE_CATEGORIES)]
            language = "French" if category == "multilingual" and len(slots) % 2 else "English"
            instruction = {
                "exact_title": "Write a concise exact-fact lookup query.",
                "paraphrase": "Write a paraphrased conceptual search query; do not copy the title.",
                "body_text": "Write a query whose answer is supported by the body rather than title words.",
                "short_natural_language": "Write a short natural-language search query.",
                "long_natural_language": "Write a detailed natural-language search query with a necessary distinction.",
                "multilingual": "Write a natural search query in the requested target language.",
                "typo_partial": "Write a realistic partial-term or supported-typo search query.",
            }[category]
            add_slot(
                category=category,
                subtype=memory_type or "fact",
                source_id=entity_id,
                title=title,
                content=content,
                family=f"entity:{entity_id}",
                instruction=instruction,
                language=language,
            )
    if len(slots) != positive_total:
        raise ValueError("could not satisfy positive source-slot count")
    negative_plan = (
        ("pure_gibberish", 60),
        ("partial_real_word_nonsense", 60),
        ("nl_off_topic", 60),
        ("false_premise", 60),
        ("fictional_unanswerable", 30),
        ("vocabulary_overlap_mismatch", 30),
    )
    negative_categories = (
        [category for category, count in negative_plan for _ in range(count)]
        if negative_total == 300
        else [
            NEGATIVE_CATEGORIES[index % len(NEGATIVE_CATEGORIES)] for index in range(negative_total)
        ]
    )
    for index, category in enumerate(negative_categories):
        number = len(slots) + 1
        slots.append(
            {
                "slot_id": f"slot-{number:04d}",
                "query_id": f"eval-{number:04d}",
                "instruction": f"Write one {category.replace('_', ' ')} query that has no answer in the supplied corpus.",
                "source_text": "",
                "target_language": "English",
                "lang": "en",
                "category": category,
                "subtype": category,
                "source_entity_ids": [],
                "topic_family_id": f"negative:{number:04d}",
                "provenance": "pending-generation",
            }
        )
    validate_slots(slots)
    return slots


def deterministic_local_generation(slots: list[dict]) -> list[dict]:  # noqa: C901, PLR0912
    """Auditable fallback when an approved external generator cannot return artifacts.

    It intentionally records `local:deterministic-fallback` provenance downstream, so these
    queries cannot be mistaken for independent LLM paraphrases in the final decision record.
    """
    results = []
    seen_queries: set[str] = set()
    for index, slot in enumerate(slots):
        title = slot["source_text"].split("\n", 1)[0].removeprefix("Title: ").strip()
        category = slot["category"]
        if category == "exact_title":
            query = title
        elif category == "paraphrase":
            query = f"What does the record about {title} explain?"
        elif category == "durable_decision":
            query = f"What documented decision, procedure, or fix concerns {title}?"
        elif category == "body_text":
            body = slot["source_text"].split("\n\n", 1)[-1].split(".", 1)[0].strip()
            query = f"Which memory explains: {body[:180]}?"
        elif category == "short_natural_language":
            query = f"What is {title}?"
        elif category == "long_natural_language":
            query = f"What is the documented guidance about {title}, and which details distinguish it from related cases?"
        elif category == "multilingual":
            query = title
        elif category == "typo_partial":
            query = perturb_typo(title, seed=index)
        elif category == "pure_gibberish":
            query = generate_gibberish_query(seed=index)
        elif category == "partial_real_word_nonsense":
            query = generate_partial_word_nonsense_query(seed=index)
        else:
            templates = {
                "nl_off_topic": "How do I train a pet dragon to repair a bicycle?",
                "false_premise": "Which documented policy requires every memory to be deleted nightly?",
                "fictional_unanswerable": "What was the color of the captain's invisible spacecraft?",
                "vocabulary_overlap_mismatch": "How does a cache invalidate a cooking recipe's flavor?",
                "current_vs_superseded": f"What is the current guidance for {title}?",
                "closely_related_incident": f"What specifically distinguishes the incident described by {title}?",
            }
            query = templates.get(category, title)
            if category in NEGATIVE_CATEGORIES:
                query = f"{query} [{index + 1}]"
        if query.casefold() in seen_queries:
            query = f"{query} [variant-{index + 1}]"
        seen_queries.add(query.casefold())
        results.append({"slot_id": slot["slot_id"], "query": query, "lang": slot["lang"]})
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slots",
        type=Path,
        help="Canonical local slots JSON; never send this private file externally.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Frozen throwaway corpus copy from which to construct private source slots.",
    )
    parser.add_argument(
        "--slots-out",
        type=Path,
        help="Write constructed private source slots here before external generation.",
    )
    parser.add_argument(
        "--batches-out",
        type=Path,
        help="Write provenance-safe external request batches while retaining private slots locally.",
    )
    parser.add_argument(
        "--public-batches-dir",
        type=Path,
        help="Directory for one safe JSON request file per batch (dev/blind kept separate).",
    )
    parser.add_argument(
        "--generated",
        type=Path,
        help="Generator response JSON with only slot_id/query/lang records.",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--corpus-fingerprint")
    parser.add_argument("--dev-target", type=int)
    parser.add_argument("--blind-target", type=int)
    parser.add_argument("--required-category", action="append", default=[])
    parser.add_argument(
        "--prepare-slots-only",
        action="store_true",
        help="Freeze private source slots and stop before any external generation.",
    )
    parser.add_argument(
        "--local-generated-out",
        type=Path,
        help="Write deterministic fallback results for the selected private slots and stop.",
    )
    parser.add_argument(
        "--local-generate-split",
        choices=("dev", "blind"),
        help="Restrict deterministic fallback generation to one already-assigned split.",
    )
    parser.add_argument(
        "--materialize-split",
        choices=("dev", "blind"),
        help="Materialize exactly one already-assigned split without reassignment.",
    )
    return parser


def _prepare_slots(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[dict]:
    if bool(args.slots) == bool(args.db_path):
        parser.error("supply exactly one of --slots or --db-path")
    if not args.db_path:
        slots_value = json.loads(args.slots.read_text())
        return (
            slots_value.get("slots", slots_value) if isinstance(slots_value, dict) else slots_value
        )

    slots = build_source_slots_from_corpus(args.db_path)
    if not args.slots_out:
        parser.error("--db-path requires --slots-out so private source selection is frozen first")
    dev_target = args.dev_target if args.dev_target is not None else 400
    blind_target = args.blind_target if args.blind_target is not None else 800
    slots = assign_slots(slots, dev_target, blind_target)
    args.slots_out.parent.mkdir(parents=True, exist_ok=True)
    args.slots_out.write_text(
        json.dumps(
            {"schema_version": 1, "slots": slots, "fingerprint": artifact_fingerprint(slots)},
            indent=2,
            ensure_ascii=False,
        )
    )
    _write_batch_artifacts(args, slots)
    if args.prepare_slots_only:
        return []
    return slots


def _write_batch_artifacts(args: argparse.Namespace, slots: list[dict]) -> None:
    if args.batches_out:
        args.batches_out.parent.mkdir(parents=True, exist_ok=True)
        args.batches_out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dev_batches": build_batches(
                        [slot for slot in slots if slot["split"] == "dev"]
                    ),
                    "blind_batches": build_batches(
                        [slot for slot in slots if slot["split"] == "blind"]
                    ),
                    "slots_fingerprint": artifact_fingerprint(slots),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    if not args.public_batches_dir:
        return
    for split in ("dev", "blind"):
        split_dir = args.public_batches_dir / split
        split_dir.mkdir(parents=True, exist_ok=True)
        for index, batch in enumerate(
            build_batches([slot for slot in slots if slot["split"] == split]), start=1
        ):
            (split_dir / f"request-{index:02d}.json").write_text(
                json.dumps({"schema_version": 1, "items": batch}, indent=2, ensure_ascii=False)
            )


def _write_local_generated(
    args: argparse.Namespace, slots: list[dict], parser: argparse.ArgumentParser
) -> bool:
    if not args.local_generated_out:
        return False
    selected = [
        slot
        for slot in slots
        if not args.local_generate_split or slot.get("split") == args.local_generate_split
    ]
    if not selected:
        parser.error("no slots matched --local-generate-split")
    results = deterministic_local_generation(selected)
    args.local_generated_out.parent.mkdir(parents=True, exist_ok=True)
    args.local_generated_out.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generator": "local:deterministic-fallback",
                "results": results,
                "slots_fingerprint": artifact_fingerprint(selected),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return True


def _write_split_manifest(
    args: argparse.Namespace,
    slots: list[dict],
    generated: list[dict],
    parser: argparse.ArgumentParser,
) -> bool:
    if not args.materialize_split:
        return False
    selected = [slot for slot in slots if slot.get("split") == args.materialize_split]
    if not selected:
        parser.error("no assigned slots matched --materialize-split")
    queries = materialize_queries(selected, generated)
    other = "blind" if args.materialize_split == "dev" else "dev"
    write_manifest(
        queries,
        args.out,
        corpus_fingerprint=args.corpus_fingerprint,
        slot_fingerprint=artifact_fingerprint(selected),
        targets={args.materialize_split: len(selected), other: 0},
        required_categories=set(args.required_category),
    )
    return True


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    slots = _prepare_slots(args, parser)
    if not slots:
        return
    if _write_local_generated(args, slots, parser):
        return
    if not args.generated:
        parser.error("--generated is required unless --prepare-slots-only is used with --db-path")
    if not args.out:
        parser.error("--out is required when materializing generated queries")
    generated_value = json.loads(args.generated.read_text())
    generated = (
        generated_value.get("results", generated_value)
        if isinstance(generated_value, dict)
        else generated_value
    )
    if isinstance(generated_value, dict) and generated_value.get("generator"):
        generated = [{**item, "provenance": generated_value["generator"]} for item in generated]
    if _write_split_manifest(args, slots, generated, parser):
        return
    if args.dev_target is None or args.blind_target is None:
        parser.error("--dev-target and --blind-target are required to freeze a manifest")
    targets = {"dev": args.dev_target, "blind": args.blind_target}
    assigned = assign_slots(slots, targets["dev"], targets["blind"])
    queries = materialize_queries(assigned, generated)
    write_manifest(
        queries,
        args.out,
        corpus_fingerprint=args.corpus_fingerprint,
        slot_fingerprint=artifact_fingerprint(assigned),
        targets=targets,
        required_categories=set(args.required_category),
    )
    return


if __name__ == "__main__":
    main()
