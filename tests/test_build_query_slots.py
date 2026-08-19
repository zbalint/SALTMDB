"""Tests for scripts/benchmarking/build_query_slots.py (Gate B query source-slots).

Fixtures are small, hand-built or programmatically-generated JSON -- never the real frozen
``scratch/eval_results/accuracy-bakeoff-20260812`` artifacts, which are too large/slow for a unit
suite and are already validated end-to-end by a manual dry run (see the task history).
"""

import hashlib
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

import bakeoff_state as bs  # noqa: E402
import build_query_slots as bqs  # noqa: E402
from build_evaluation_queries import assign_slots, load_manifest, materialize_queries  # noqa: E402


# ---------------------------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------------------------


def _entity_row(entity_id: str, title: str, body: str) -> dict:
    """Manifest row matching how ``build_query_slots._normalize``/``_sha256_text`` hash text."""
    norm_title = bqs._normalize(title)
    norm_body = bqs._normalize(body)
    return {
        "entity_id": entity_id,
        "title_hash": bqs._sha256_text(norm_title),
        "body_hash": bqs._sha256_text(norm_body),
        "source_hash": bqs._sha256_text(f"{norm_title}|{norm_body}"),
        "chunk_hashes": [bqs._sha256_text(norm_body[:50] or "chunk")],
    }


def _write_corpus(
    tmp_path: Path, entities: list[tuple[str, str, str]], edges=()
) -> tuple[Path, Path]:
    """Write a synthetic ``corpus_export.json`` + validly-signed manifest for ``entities``."""
    ids = sorted(entity_id for entity_id, _title, _body in entities)
    by_id = {entity_id: (title, body) for entity_id, title, body in entities}
    rows = [_entity_row(entity_id, *by_id[entity_id]) for entity_id in ids]
    representation_version = "gate-b-test-v1"
    corpus_root_hash = bs.fingerprint(
        {"eligible_ids": ids, "entities": rows, "representation_version": representation_version}
    )
    manifest = bs.sign_artifact(
        "CorpusRepresentationManifest",
        {
            "eligible_ids": ids,
            "entities": rows,
            "representation_version": representation_version,
            "corpus_root_hash": corpus_root_hash,
        },
    )
    export = {
        "entities": [
            {"entity_id": entity_id, "title": by_id[entity_id][0], "body": by_id[entity_id][1]}
            for entity_id in ids
        ],
        "supersedes_edges": list(edges),
    }
    export_path = tmp_path / "corpus_export.json"
    manifest_path = tmp_path / "corpus_representation_manifest.json"
    export_path.write_text(json.dumps(export, ensure_ascii=False))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    return export_path, manifest_path


def _full_entity_pool() -> tuple[list[tuple[str, str, str]], list[dict]]:
    """The minimum entity/edge inventory ``build_source_slots_from_export`` needs to succeed.

    30 current/superseded pairs (60 entities) + 30 close_sibling pairs (60 entities) + 30
    entities apiece for the six remaining "simple" entity facets (180 entities) = 300 total,
    matching ``FACET_TARGETS``'s uniform 30-entity-per-facet, two-variant-per-entity design.
    """
    entities: list[tuple[str, str, str]] = []
    edges: list[dict] = []

    # current_vs_superseded: 30 pairs. Mid-length bodies so they never rank as the corpus-wide
    # shortest/longest 30 (which would otherwise leak them into the short_memory/long_body pools).
    for i in range(30):
        cur_id, old_id = f"cur-{i:04d}", f"old-{i:04d}"
        entities.append(
            (
                cur_id,
                f"Active configuration item {i} details",
                f"Active configuration item {i} carries the current guidance text for this row.",
            )
        )
        entities.append(
            (
                old_id,
                f"Deprecated configuration item {i} details",
                f"Deprecated configuration item {i} carries the prior guidance text for this row.",
            )
        )
        edges.append({"target_id": cur_id, "source_id": old_id})

    # close_sibling: 30 pairs, each sharing exactly one unique 4-letter tag token in its title so
    # it (and only it) falls into find_sibling_pairs's 2-4-shared-title rare-token band.
    tags = [
        "".join(letters)
        for letters in itertools.islice(
            itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=4), 30
        )
    ]
    for i, tag in enumerate(tags):
        a_id, b_id = f"sib-{i:04d}a", f"sib-{i:04d}b"
        entities.append(
            (
                a_id,
                f"Reference memory alpha {tag} record",
                f"Reference memory alpha {tag} record holds a short supporting note.",
            )
        )
        entities.append(
            (
                b_id,
                f"Reference memory beta {tag} record",
                f"Reference memory beta {tag} record holds a short supporting note.",
            )
        )

    # short_memory pool: 30 entities with distinctly short bodies.
    for i in range(30):
        entities.append((f"short-{i:04d}", f"Short memory topic {i}", f"Brief note {i}."))

    # long_body pool: 30 entities with distinctly long bodies.
    for i in range(30):
        long_body = (
            f"Long body topic {i} discusses a distinguishing detail worth remembering in depth. "
            + ("Supporting elaboration continues here to pad the body past the short pool. " * 8)
        )
        entities.append((f"long-{i:04d}", f"Long memory topic {i}", long_body))

    # exact_sentence / keyword / typo / multilingual pools: 30 x 4 = 120 entities.
    for i in range(120):
        entities.append(
            (
                f"mid-{i:04d}",
                f"Mid memory topic {i}",
                f"This is a distinguishing detail about memory topic {i} that forms one complete "
                f"sentence. Additional filler content follows to give the body more context.",
            )
        )

    return entities, edges


def _synthetic_corpus(tmp_path: Path) -> tuple[Path, Path]:
    entities, edges = _full_entity_pool()
    return _write_corpus(tmp_path, entities, edges)


# ---------------------------------------------------------------------------------------------
# 1. Module-level invariants
# ---------------------------------------------------------------------------------------------


def test_facet_targets_keys_match_mandatory_facets_exactly():
    assert set(bqs.FACET_TARGETS["dev"]) == bs.MANDATORY_FACETS
    assert set(bqs.FACET_TARGETS["blind"]) == bs.MANDATORY_FACETS


def test_facet_targets_sum_to_frozen_split_totals():
    assert sum(bqs.FACET_TARGETS["dev"].values()) == 400
    assert sum(bqs.FACET_TARGETS["blind"].values()) == 800


def test_facet_instructions_cover_mandatory_facets_exactly():
    assert set(bqs.FACET_INSTRUCTIONS) == bs.MANDATORY_FACETS
    for instruction in bqs.FACET_INSTRUCTIONS.values():
        assert isinstance(instruction, str) and instruction.strip()


def test_generation_prompt_hash_is_stable_lowercase_sha256_of_prompt():
    assert bqs.GENERATION_PROMPT_HASH == hashlib.sha256(bqs.GENERATION_PROMPT.encode()).hexdigest()
    assert bs.SHA256_RE.fullmatch(bqs.GENERATION_PROMPT_HASH) is not None
    assert bqs.GENERATION_PROMPT_HASH == bqs.GENERATION_PROMPT_HASH.lower()


# ---------------------------------------------------------------------------------------------
# 2. load_export_bound_to_manifest
# ---------------------------------------------------------------------------------------------


def test_load_export_bound_to_manifest_happy_path(tmp_path):
    entities = [
        ("e1", "Title One", "Body one text."),
        ("e2", "Title Two", "Body two text."),
    ]
    export_path, manifest_path = _write_corpus(tmp_path, entities)
    loaded, corpus_root_hash = bqs.load_export_bound_to_manifest(export_path, manifest_path)
    manifest = bs.validate_corpus_manifest(json.loads(manifest_path.read_text()))
    assert corpus_root_hash == manifest["corpus_root_hash"]
    assert set(loaded) == {"e1", "e2"}
    assert loaded["e1"] == {"title": "Title One", "body": "Body one text."}


def test_load_export_bound_to_manifest_rejects_tampered_title(tmp_path):
    entities = [
        ("e1", "Title One", "Body one text."),
        ("e2", "Title Two", "Body two text."),
    ]
    export_path, manifest_path = _write_corpus(tmp_path, entities)
    export = json.loads(export_path.read_text())
    export["entities"][0]["title"] = "Tampered Title"
    export_path.write_text(json.dumps(export))
    with pytest.raises(bqs.GateBSlotError, match="does not match the signed manifest hash"):
        bqs.load_export_bound_to_manifest(export_path, manifest_path)


def test_load_export_bound_to_manifest_rejects_tampered_body(tmp_path):
    entities = [
        ("e1", "Title One", "Body one text."),
        ("e2", "Title Two", "Body two text."),
    ]
    export_path, manifest_path = _write_corpus(tmp_path, entities)
    export = json.loads(export_path.read_text())
    export["entities"][1]["body"] = "Tampered body text."
    export_path.write_text(json.dumps(export))
    with pytest.raises(bqs.GateBSlotError, match="does not match the signed manifest hash"):
        bqs.load_export_bound_to_manifest(export_path, manifest_path)


def test_load_export_bound_to_manifest_rejects_entity_id_set_mismatch(tmp_path):
    entities = [
        ("e1", "Title One", "Body one text."),
        ("e2", "Title Two", "Body two text."),
    ]
    export_path, manifest_path = _write_corpus(tmp_path, entities)
    export = json.loads(export_path.read_text())
    export["entities"].append({"entity_id": "e3", "title": "Title Three", "body": "Body three."})
    export_path.write_text(json.dumps(export))
    with pytest.raises(bqs.GateBSlotError, match="do not match the signed manifest"):
        bqs.load_export_bound_to_manifest(export_path, manifest_path)


# ---------------------------------------------------------------------------------------------
# 3. find_sibling_pairs
# ---------------------------------------------------------------------------------------------


def test_find_sibling_pairs_matches_shared_rare_tokens_and_ignores_unrelated_entities():
    entities = {
        "a": {"title": "Zephyr uniquewordone", "body": ""},
        "b": {"title": "Zephyr uniquewordtwo", "body": ""},
        "c": {"title": "Wombat uniquewordthree", "body": ""},
        "d": {"title": "Wombat uniquewordfour", "body": ""},
        "e": {"title": "Completely unrelated entry", "body": ""},
    }
    pairs = bqs.find_sibling_pairs(entities, excluded=set(), needed=5)
    assert set(pairs) == {("a", "b"), ("c", "d")}
    assert "e" not in {member for pair in pairs for member in pair}


def test_find_sibling_pairs_never_reuses_an_entity_across_two_pairs():
    # "gizmo" is shared by three titles (in the 2-4 rare-token band), producing three candidate
    # pairs; only one may be selected since each entity can back at most one pair.
    entities = {
        "x": {"title": "Gizmo alpha widget", "body": ""},
        "y": {"title": "Gizmo beta widget", "body": ""},
        "z": {"title": "Gizmo gamma widget", "body": ""},
    }
    pairs = bqs.find_sibling_pairs(entities, excluded=set(), needed=5)
    assert len(pairs) == 1
    members = {member for pair in pairs for member in pair}
    assert len(members) == 2


def test_find_sibling_pairs_respects_excluded_set():
    entities = {
        "a": {"title": "Zephyr uniquewordone", "body": ""},
        "b": {"title": "Zephyr uniquewordtwo", "body": ""},
    }
    pairs = bqs.find_sibling_pairs(entities, excluded={"a"}, needed=5)
    assert pairs == []


def test_find_sibling_pairs_respects_needed_cap():
    entities = {
        "a": {"title": "Zephyr uniquewordone", "body": ""},
        "b": {"title": "Zephyr uniquewordtwo", "body": ""},
        "c": {"title": "Wombat uniquewordthree", "body": ""},
        "d": {"title": "Wombat uniquewordfour", "body": ""},
    }
    pairs = bqs.find_sibling_pairs(entities, excluded=set(), needed=1)
    assert len(pairs) == 1


# ---------------------------------------------------------------------------------------------
# 4. pick_supersedes_families
# ---------------------------------------------------------------------------------------------


def test_pick_supersedes_families_skips_self_loops_dup_targets_and_missing_entities():
    entities = {"a": {}, "b": {}, "c": {}, "d": {}}
    export = {
        "supersedes_edges": [
            {"target_id": "a", "source_id": "a"},  # self-loop
            {"target_id": "a", "source_id": "missing2"},  # source absent from entities
            {"target_id": "b", "source_id": "c"},  # valid
            {"target_id": "b", "source_id": "d"},  # target already used
            {"target_id": "missing", "source_id": "a"},  # target absent from entities
        ]
    }
    families = bqs.pick_supersedes_families(export, entities, needed=10)
    assert families == [("b", "c")]


def test_pick_supersedes_families_respects_needed_cap():
    entities = {f"cur{i}": {} for i in range(5)} | {f"old{i}": {} for i in range(5)}
    export = {
        "supersedes_edges": [{"target_id": f"cur{i}", "source_id": f"old{i}"} for i in range(5)]
    }
    families = bqs.pick_supersedes_families(export, entities, needed=3)
    assert len(families) == 3
    assert families == sorted(families)


def test_pick_supersedes_families_returns_empty_without_usable_edges():
    entities = {"a": {}, "b": {}}
    export = {"supersedes_edges": [{"target_id": "a", "source_id": "a"}]}
    assert bqs.pick_supersedes_families(export, entities, needed=5) == []
    assert bqs.pick_supersedes_families({}, entities, needed=5) == []


# ---------------------------------------------------------------------------------------------
# 5. _variant_schedule
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "num_families,total_target",
    [(1, 1), (3, 6), (5, 12), (7, 7), (4, 10), (30, 60)],
)
def test_variant_schedule_sums_exactly_and_is_all_positive(num_families, total_target):
    schedule = bqs._variant_schedule(num_families, total_target)
    assert len(schedule) == num_families
    assert sum(schedule) == total_target
    assert all(count > 0 for count in schedule)


def test_variant_schedule_remainder_distribution_is_front_loaded():
    # base, remainder = divmod(10, 4) = (2, 2) -> first two families get +1.
    assert bqs._variant_schedule(4, 10) == [3, 3, 2, 2]


def test_variant_schedule_rejects_target_smaller_than_family_count():
    with pytest.raises(bqs.GateBSlotError):
        bqs._variant_schedule(5, 4)


def test_variant_schedule_rejects_non_positive_family_count():
    with pytest.raises(bqs.GateBSlotError):
        bqs._variant_schedule(0, 10)
    with pytest.raises(bqs.GateBSlotError):
        bqs._variant_schedule(-1, 10)


# ---------------------------------------------------------------------------------------------
# 6. build_source_slots_from_export -> assign_slots -> materialize -> validate, end to end
# ---------------------------------------------------------------------------------------------


def test_build_source_slots_from_export_produces_exactly_1200_valid_slots(tmp_path):
    export_path, manifest_path = _synthetic_corpus(tmp_path)
    slots, corpus_root_hash = bqs.build_source_slots_from_export(export_path, manifest_path)
    assert len(slots) == 1200
    manifest = bs.validate_corpus_manifest(json.loads(manifest_path.read_text()))
    assert corpus_root_hash == manifest["corpus_root_hash"]
    facet_counts: dict[str, int] = {}
    for slot in slots:
        facet_counts[slot["category"]] = facet_counts.get(slot["category"], 0) + 1
    for facet in bs.MANDATORY_FACETS:
        assert facet_counts[facet] == (
            bqs.FACET_TARGETS["dev"][facet] + bqs.FACET_TARGETS["blind"][facet]
        )


def test_gate_b_pipeline_end_to_end(tmp_path):
    export_path, manifest_path = _synthetic_corpus(tmp_path)
    slots, corpus_root_hash = bqs.build_source_slots_from_export(export_path, manifest_path)

    assigned = assign_slots(slots, 400, 800, category_targets=bqs.FACET_TARGETS)
    assert len(assigned) == 1200
    counts = {"dev": 0, "blind": 0}
    facet_counts = {"dev": {}, "blind": {}}
    for slot in assigned:
        counts[slot["split"]] += 1
        facet_counts[slot["split"]][slot["category"]] = (
            facet_counts[slot["split"]].get(slot["category"], 0) + 1
        )
    assert counts == {"dev": 400, "blind": 800}
    assert facet_counts["dev"] == bqs.FACET_TARGETS["dev"]
    assert facet_counts["blind"] == bqs.FACET_TARGETS["blind"]

    dev_slots = [slot for slot in assigned if slot["split"] == "dev"]
    generated = bqs.deterministic_local_generation(dev_slots)
    queries = materialize_queries(dev_slots, generated)
    assert len(queries) == 400
    assert {query["category"] for query in queries} == set(bs.MANDATORY_FACETS)

    assignments = bqs.build_query_slot_assignments(assigned)
    validated = bs.validate_query_slot_assignments(assignments)
    assert len(validated["assignments"]) == 1200
    dev_facets = {row["facet"] for row in validated["assignments"] if row["split"] == "dev"}
    blind_facets = {row["facet"] for row in validated["assignments"] if row["split"] == "blind"}
    assert dev_facets == bs.MANDATORY_FACETS
    assert blind_facets == bs.MANDATORY_FACETS

    # Dev and blind never share a topic_family_id or a source_entity_ids member.
    family_split: dict[str, str] = {}
    source_split: dict[str, str] = {}
    for row in validated["assignments"]:
        family_split.setdefault(row["topic_family_id"], row["split"])
        assert family_split[row["topic_family_id"]] == row["split"]
        for source_id in row["source_entity_ids"]:
            source_split.setdefault(source_id, row["split"])
            assert source_split[source_id] == row["split"]


# ---------------------------------------------------------------------------------------------
# 7. Failure paths: too-small frozen exports raise GateBSlotError, not a malformed result
# ---------------------------------------------------------------------------------------------


def test_build_source_slots_rejects_missing_supersedes_pairs(tmp_path):
    entities, _edges = _full_entity_pool()
    export_path, manifest_path = _write_corpus(tmp_path, entities, edges=[])
    with pytest.raises(bqs.GateBSlotError, match="supersedes"):
        bqs.build_source_slots_from_export(export_path, manifest_path)


def test_build_source_slots_rejects_insufficient_sibling_pairs(tmp_path):
    entities, edges = _full_entity_pool()
    # Break sibling pairing: give every "sib-*" entity a title with no shared rare token.
    entities = [
        (entity_id, f"Unrelated singular title for {entity_id}", body)
        if entity_id.startswith("sib-")
        else (entity_id, title, body)
        for entity_id, title, body in entities
    ]
    export_path, manifest_path = _write_corpus(tmp_path, entities, edges=edges)
    with pytest.raises(bqs.GateBSlotError, match="close_sibling"):
        bqs.build_source_slots_from_export(export_path, manifest_path)


def test_build_source_slots_rejects_insufficient_simple_facet_entities(tmp_path):
    entities, edges = _full_entity_pool()
    entities = [row for row in entities if not row[0].startswith(("short-", "long-", "mid-"))]
    export_path, manifest_path = _write_corpus(tmp_path, entities, edges=edges)
    with pytest.raises(bqs.GateBSlotError, match="simple facets"):
        bqs.build_source_slots_from_export(export_path, manifest_path)


# ---------------------------------------------------------------------------------------------
# 8. deterministic_local_generation
# ---------------------------------------------------------------------------------------------


def test_deterministic_local_generation_covers_every_facet_with_nonempty_text():
    slots = [
        {
            "slot_id": "s-exact",
            "category": "exact_sentence",
            "source_text": "Verbatim sentence text.",
            "lang": "en",
        },
        {
            "slot_id": "s-keyword",
            "category": "keyword",
            "source_text": "Title: Sample Title\n\nBody content about a specific distinguishing detail here.",
            "lang": "en",
        },
        {
            "slot_id": "s-typo",
            "category": "typo",
            "source_text": "Sample Title Words",
            "lang": "en",
        },
        {
            "slot_id": "s-short",
            "category": "short_memory",
            "source_text": "Sample Title Words",
            "lang": "en",
        },
        {
            "slot_id": "s-long",
            "category": "long_body",
            "source_text": "Title: Sample Title\n\nBody content about a specific distinguishing detail here.",
            "lang": "en",
        },
        {
            "slot_id": "s-cvs",
            "category": "current_vs_superseded",
            "source_text": "Current title: New Guidance\nSuperseded title: Old Guidance",
            "lang": "en",
        },
        {
            "slot_id": "s-sibling",
            "category": "close_sibling",
            "source_text": "Title A: First Title\nTitle B: Second Title",
            "lang": "en",
        },
        {
            "slot_id": "s-multi",
            "category": "multilingual",
            "source_text": "Titre Exemple",
            "lang": "und",
        },
        {
            "slot_id": "s-neg1",
            "category": "strict_negative",
            "subtype": "pure_gibberish",
            "source_text": "No corpus content is required for this negative probe.",
            "lang": "en",
        },
        {
            "slot_id": "s-neg2",
            "category": "strict_negative",
            "subtype": "false_premise",
            "source_text": "No corpus content is required for this negative probe.",
            "lang": "en",
        },
    ]
    results = bqs.deterministic_local_generation(slots)
    assert len(results) == len(slots)
    assert {row["slot_id"] for row in results} == {slot["slot_id"] for slot in slots}
    for row in results:
        assert isinstance(row["query"], str) and row["query"].strip()


def test_deterministic_local_generation_dedups_identical_text_with_variant_suffix():
    slots = [
        {
            "slot_id": "s1",
            "category": "exact_sentence",
            "source_text": "Repeated line.",
            "lang": "en",
        },
        {
            "slot_id": "s2",
            "category": "exact_sentence",
            "source_text": "Repeated line.",
            "lang": "en",
        },
    ]
    results = bqs.deterministic_local_generation(slots)
    texts = [row["query"] for row in results]
    assert texts[0] == "Repeated line."
    assert texts[1] == "Repeated line. [variant-2]"
    assert texts[0].casefold() != texts[1].casefold()


# ---------------------------------------------------------------------------------------------
# 9. --dev-out CLI provenance regression (the Gate D StaleArtifactError fix)
# ---------------------------------------------------------------------------------------------


def test_dev_out_cli_produces_signed_provenance_not_legacy_unbound(tmp_path, monkeypatch):
    """``build_query_slots.py --dev-out`` must emit a real signed provenance envelope.

    Regression test for the bug where ``main()`` called ``write_manifest`` without
    ``commit_fingerprint``/``random_seed``/``config_fingerprint``/``judge_version_fingerprint``,
    so every produced dev manifest silently fell back to ``{"status": "legacy_unbound", "stale":
    True}`` and was unconditionally rejected by
    ``build_evaluation_queries.load_manifest(..., require_provenance=True)`` --
    ``run_retrieval_bakeoff.py``'s hardcoded consumer call.
    """
    export_path, manifest_path = _synthetic_corpus(tmp_path)
    slots_out = tmp_path / "source_slots.json"
    assignments_out = tmp_path / "query_slot_assignments.json"
    dev_out = tmp_path / "queries_dev.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_query_slots.py",
            "--export",
            str(export_path),
            "--manifest",
            str(manifest_path),
            "--slots-out",
            str(slots_out),
            "--assignments-out",
            str(assignments_out),
            "--dev-out",
            str(dev_out),
        ],
    )
    bqs.main()

    manifest = json.loads(dev_out.read_text())
    provenance = manifest["provenance"]
    assert provenance.get("status") != "legacy_unbound"
    assert provenance["schema_version"] == 1
    assert provenance["random_seed"] == bqs.DEV_MANIFEST_RANDOM_SEED
    assert provenance["config_fingerprint"] == bqs.GENERATION_PROMPT_HASH

    # The real consumer path (run_retrieval_bakeoff.py:313) must accept this manifest.
    loaded = load_manifest(dev_out, expected_split="dev", require_provenance=True)
    assert loaded["provenance"]["schema_version"] == 1
    assert len(loaded["queries"]) == 400
