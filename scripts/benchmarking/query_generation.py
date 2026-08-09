"""Pure, DB-free query-generation building blocks for the precision-first search evaluation
(`scratch/plans/precision_first_search_evaluation.md`, §2). Programmatic pieces (§2c: "Claude,
zero LLM cost, deterministic") live here: typo/partial-term perturbation, gibberish/partial-
nonsense negatives, recency/length bucketing, `topic_family_id` computation, and the
dev/blind split-assignment algorithm (§2b's hard rule -- whole families, never split).

The orchestration script that actually reads the frozen corpus, calls
`squad_question_extractor.py`, dispatches LLM-generation batches, and writes
`queries_{dev,blind}.json` is a separate, not-yet-built piece -- this module is its pure-logic
dependency, kept import-free of any DB/CADET/codex call so it's unit-testable now.
"""

import random
import re
import unicodedata
from dataclasses import asdict, dataclass

# Same convention as benchmark_precision_snapshot.py's LENGTH_BUCKETS -- reused for query-text
# (not entity-content) length classification here, so "short/long NL queries" bucketing uses one
# consistent scheme across this repo's benchmarking tooling.
QUERY_LENGTH_BUCKETS = [("short", 0, 40), ("medium", 40, 100), ("long", 100, None)]


@dataclass
class QueryRow:
    """One row of queries_{dev,blind}.json, per plan §2a's declared schema."""

    id: str
    query: str
    lang: str
    category: str
    subtype: str
    split: str  # "dev" | "blind"
    source_entity_ids: list[str]
    topic_family_id: str
    length_bucket: str
    provenance: str  # "programmatic" | "llm:claude" | "llm:cadet-gemini-flash" | "llm:codex" | "squad-ground-truth"

    def to_dict(self) -> dict:
        return asdict(self)


def classify_length_bucket(query_text: str) -> str:
    n = len(query_text)
    for name, lo, hi in QUERY_LENGTH_BUCKETS:
        if n >= lo and (hi is None or n < hi):
            return name
    return "unknown"


# ---------------------------------------------------------------------------------------------
# §2c: typo / partial-term perturbation (deterministic given a seed)
# ---------------------------------------------------------------------------------------------


def perturb_typo(text: str, seed: int, n_edits: int = 1) -> str:
    """Applies n_edits character-level edits (swap-adjacent, drop, or duplicate — never an edit
    that changes text length by more than 1 char per edit) to `text`, picked deterministically
    from `seed`. Only touches alphabetic characters (skips spaces/punctuation as edit sites) so
    the result stays a recognizable, word-boundary-preserving near-miss, not gibberish."""
    rng = random.Random(seed)
    chars = list(text)
    editable_positions = [
        i for i, c in enumerate(chars) if c.isalpha() and i + 1 < len(chars) and chars[i + 1].isalpha()
    ]
    if not editable_positions:
        return text  # nothing safe to edit (e.g. very short/no-letters text)

    for _ in range(n_edits):
        if not editable_positions:
            break
        pos = rng.choice(editable_positions)
        edit_kind = rng.choice(["swap", "drop", "duplicate"])
        if edit_kind == "swap" and pos + 1 < len(chars):
            chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
        elif edit_kind == "drop":
            del chars[pos]
        elif edit_kind == "duplicate":
            chars.insert(pos, chars[pos])
        editable_positions = [
            i
            for i, c in enumerate(chars)
            if c.isalpha() and i + 1 < len(chars) and chars[i + 1].isalpha()
        ]
    return "".join(chars)


def truncate_to_partial_terms(text: str, seed: int, keep_fraction: float = 0.6) -> str:
    """Drops a random subset of whitespace-delimited tokens (keeping their relative order),
    simulating a user typing partial/incomplete search terms. keep_fraction is a target, not
    exact (rounds to at least 1 token kept)."""
    tokens = text.split()
    if len(tokens) <= 1:
        return text
    rng = random.Random(seed)
    n_keep = max(1, round(len(tokens) * keep_fraction))
    keep_indices = set(rng.sample(range(len(tokens)), n_keep))
    return " ".join(tok for i, tok in enumerate(tokens) if i in keep_indices)


# ---------------------------------------------------------------------------------------------
# §2a negatives: pure gibberish + partial-real-word nonsense (deterministic, no LLM)
# ---------------------------------------------------------------------------------------------

_CONSONANTS = "bcdfghjklmnpqrstvwxyz"
_VOWELS = "aeiou"

# A small, fixed set of real English morphemes/word-fragments -- combined into nonsense that
# LOOKS like it could be a real word/phrase but isn't (distinct from pure gibberish, which uses
# no real linguistic material at all). Matches the source plan's explicit split between "pure
# gibberish" and "partial-real-word nonsense" negative categories.
_REAL_FRAGMENTS = [
    "photo", "graph", "bio", "logy", "micro", "scope", "trans", "port",
    "hydro", "electro", "static", "dynamic", "auto", "matic", "super",
    "hyper", "para", "meta", "phase", "cycle", "therm", "al", "istic",
]


def generate_gibberish_query(seed: int, n_tokens: int = 3) -> str:
    """Deterministic pure-gibberish query: consonant-vowel-consonant pseudo-tokens with no real
    linguistic content at all."""
    rng = random.Random(seed)
    tokens = []
    for _ in range(n_tokens):
        length = rng.randint(4, 8)
        token = "".join(
            rng.choice(_CONSONANTS if i % 2 == 0 else _VOWELS) for i in range(length)
        )
        tokens.append(token)
    return " ".join(tokens) + "?"


def generate_partial_word_nonsense_query(seed: int, n_fragments: int = 3) -> str:
    """Deterministic partial-real-word nonsense: real English morpheme fragments recombined into
    a phrase with no coherent meaning (distinct from pure gibberish -- these fragments are real,
    the combination isn't)."""
    rng = random.Random(seed)
    fragments = rng.sample(_REAL_FRAGMENTS, min(n_fragments, len(_REAL_FRAGMENTS)))
    return " ".join(fragments) + " thing?"


# ---------------------------------------------------------------------------------------------
# §0b item 8 / §2b: topic_family_id computation
# ---------------------------------------------------------------------------------------------


def compute_topic_family_id(
    dataset: str | None, source_title: str | None, entity_id: str, cluster_root_id: str | None = None
) -> str:
    """One topic_family_id per underlying source/topic, per §2b's hard rule. Three cases, in
    priority order:
    1. `cluster_root_id` given (e.g. a supersession chain's current/authoritative entity id, or
       an elaborates_on/similar_to/resolves incident-family's designated root) -- every entity in
       that cluster shares this same family id, regardless of dataset/title.
    2. `dataset` + `source_title` given (squad/wikipedia entities) -- reuses
       build_diverse_test_db.py's own `f"{dataset}:{source_title}"` convention exactly, so this
       plan's family grouping never collides with or contradicts that script's own
       split_group_id for the same entities.
    3. Neither -- falls back to the entity's own id (a singleton family of one)."""
    if cluster_root_id:
        return f"cluster:{cluster_root_id}"
    if dataset and source_title:
        return f"{dataset}:{source_title}"
    return f"entity:{entity_id}"


# ---------------------------------------------------------------------------------------------
# §2b: dev/blind split assignment -- whole families, never split, deterministic
# ---------------------------------------------------------------------------------------------


def assign_families_to_split(
    family_query_counts: dict[str, int],
    dev_target: int,
    blind_target: int,
    seed: int = 0,
) -> dict[str, str]:
    """Deterministically assigns each topic_family_id to "dev" XOR "blind" (never split), trying
    to hit dev_target/blind_target total query counts as closely as whole-family granularity
    allows. Greedy largest-first bin assignment: sort families by query count descending (ties
    broken by family_id for determinism), assign each to whichever bin currently has more
    remaining capacity (target - already-assigned). Returns {family_id: "dev"|"blind"}."""
    families = sorted(family_query_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    assigned: dict[str, str] = {}
    dev_used = 0
    blind_used = 0
    for family_id, count in families:
        dev_remaining = dev_target - dev_used
        blind_remaining = blind_target - blind_used
        if dev_remaining >= blind_remaining:
            assigned[family_id] = "dev"
            dev_used += count
        else:
            assigned[family_id] = "blind"
            blind_used += count
    return assigned
