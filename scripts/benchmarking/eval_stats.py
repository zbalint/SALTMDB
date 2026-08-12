"""Pure statistics/metric functions for the precision-first search evaluation
(`scratch/plans/precision_first_search_evaluation.md`, §5) -- imported by
`analyze_evaluation_matrix.py`. Kept import-free of any DB/CADET/codex dependency so it's cheap
to unit-test exhaustively (see `tests/test_eval_stats.py`) without any real query/judgment data.

Every formula here is deliberately literal against the plan's §5a-5h text -- this module exists
specifically because Codex round 2/3 review found the *prose* version of these formulas
underspecified/ambiguous three separate times; this is the disambiguated, executable form.

## Data model this module operates on (see docstrings below for exact shapes)
A "config run" for one query is a `ranked_ids: list[str]` -- the candidate entity ids that
config returned for that query, in rank order (index 0 = rank 1), truncated to whatever length
the caller passes in (§0b item 4: callers pass the real `search_memory(limit=20)` output; this
module always scores against the first 10 of whatever list it's given, per §5a).

A query's "relevance set" (§5a "Relevance-set unification") is `dict[str, int]` mapping every
candidate id in that query's UNIFIED pool (pooled-across-all-configs candidates plus any
force-included ground-truth ids, §0b item 17) to its merged judge grade (0/1/2). This SAME dict
is used for every config's scoring of that query -- never rebuilt per-config.
"""

import math
import random
import statistics
from dataclasses import dataclass, field


# Frozen judging contract: grades are 0/1/2 and NDCG gains are 0/1/3.  Exporting the mapping
# makes downstream promotion/report code use the same gain function rather than duplicating an
# accidental linear (0/1/2) scale.
JUDGMENT_GRADES = (0, 1, 2)
NDCG_GAINS = {0: 0, 1: 1, 2: 3}


# ---------------------------------------------------------------------------------------------
# §5a: per-query, per-config metric primitives
# ---------------------------------------------------------------------------------------------


def _gain(grade: int) -> float:
    """g(grade) = 2^grade - 1, per §5a."""
    if grade not in NDCG_GAINS:
        raise ValueError("judgment grade must be 0, 1, or 2")
    return float(NDCG_GAINS[grade])


def dcg_at_10(ranked_ids: list[str], relevance: dict[str, int]) -> float:
    """DCG@10 over a config's own top-10, using the query's unified relevance dict. An id in
    ranked_ids with no entry in `relevance` (never pooled/graded) contributes 0 gain -- this
    should not happen for ids a config actually returned (they'd have been pooled), but is
    handled defensively rather than raising."""
    total = 0.0
    for rank, cand_id in enumerate(ranked_ids[:10], start=1):
        grade = relevance.get(cand_id, 0)
        total += _gain(grade) / math.log2(rank + 1)
    return total


def idcg_at_10(relevance: dict[str, int]) -> float:
    """IDCG@10 from the query's own unified relevance set, sorted by grade descending, ties
    broken by candidate id ascending for full determinism (§5a). SAME value for every config on
    a given query -- callers should compute this once per query, not once per (query, config)."""
    ranked = sorted(relevance.items(), key=lambda kv: (-kv[1], kv[0]))
    total = 0.0
    for rank, (_cand_id, grade) in enumerate(ranked[:10], start=1):
        total += _gain(grade) / math.log2(rank + 1)
    return total


def ndcg_at_10(ranked_ids: list[str], relevance: dict[str, int]) -> float | None:
    """Returns None (not 0.0) when IDCG is 0 -- i.e. the query's relevance set has no positively-
    graded item at all, so NDCG is undefined for it, not trivially 0. Callers average over
    non-None values only (matches §5a's pooled-Recall "excluded, not scored as 0" convention,
    applied consistently to NDCG for the same reason)."""
    idcg = idcg_at_10(relevance)
    if idcg == 0.0:
        return None
    return dcg_at_10(ranked_ids, relevance) / idcg


def mrr(ranked_ids: list[str], relevance: dict[str, int], grade_threshold: int) -> float:
    """1/rank of the first top-10 candidate with merged grade >= grade_threshold, else 0.
    grade_threshold=2 is "MRR (primary)"; grade_threshold=1 is "MRR@>=1 (secondary)", per §5a."""
    for rank, cand_id in enumerate(ranked_ids[:10], start=1):
        if relevance.get(cand_id, 0) >= grade_threshold:
            return 1.0 / rank
    return 0.0


def pooled_recall_at_10(ranked_ids: list[str], relevance: dict[str, int]) -> float | None:
    """|grade==2 items in top-10| / |grade==2 items in the unified relevance set|. None (not 0 or
    1) when the relevance set has zero grade==2 items -- §5a: "excluded... not scored as 0 or 1,
    undefined, not vacuous-pass." """
    total_relevant = sum(1 for g in relevance.values() if g == 2)
    if total_relevant == 0:
        return None
    top10 = set(ranked_ids[:10])
    hit = sum(1 for cand_id, g in relevance.items() if g == 2 and cand_id in top10)
    return hit / total_relevant


def semantic_recall_at_20(ranked_ids: list[str], relevance: dict[str, int]) -> float | None:
    """Grade-2 semantic recall@20 used by the Stage-1 promotion gate.

    This is the same unified relevance denominator as :func:`pooled_recall_at_10`, but evaluates
    the candidate's first twenty results.  ``None`` means no grade-2 judged item exists for the
    query and is excluded from an aggregate rather than treated as a vacuous pass.
    """
    total_relevant = sum(1 for grade in relevance.values() if grade == 2)
    if total_relevant == 0:
        return None
    top20 = set(ranked_ids[:20])
    hit = sum(
        1 for candidate_id, grade in relevance.items() if grade == 2 and candidate_id in top20
    )
    return hit / total_relevant


# Short aliases used by promotion/report code and hidden fixture consumers.
recall_at_20 = semantic_recall_at_20
grade2_semantic_recall_at_20 = semantic_recall_at_20


def known_answer_recall_at_10(ranked_ids: list[str], source_entity_ids: list[str]) -> float | None:
    """1 if any predeclared source_entity_ids appears in the config's own top-10, else 0. None if
    the query has no predeclared ground truth at all (excluded from this metric's denominator,
    §5a -- LLM-paraphrase-only categories lack this by construction)."""
    if not source_entity_ids:
        return None
    top10 = set(ranked_ids[:10])
    return 1.0 if any(sid in top10 for sid in source_entity_ids) else 0.0


def false_accept_rate_flag(ranked_ids: list[str], relevance: dict[str, int]) -> bool:
    """True iff this negative query's top-1 has merged grade >= 1 (§5a: "a judge decided the
    returned result actually means something related"). Caller averages this over the negative
    query set to get the false-accept rate."""
    if not ranked_ids:
        return False
    top1_grade = relevance.get(ranked_ids[0], 0)
    return top1_grade >= 1


def misleading_top1_flag(ranked_ids: list[str], relevance: dict[str, int]) -> bool:
    """True iff top-1 has merged grade == 0. Computed over ALL queries (positive + negative),
    §5a."""
    if not ranked_ids:
        return False
    return relevance.get(ranked_ids[0], 0) == 0


def top1_direct_relevance_flag(ranked_ids: list[str], relevance: dict[str, int]) -> bool:
    """True iff this positive query's top-1 has merged grade == 2 (§5a "Top-1 direct-relevance
    rate" / the McNemar binary outcome used throughout §5d-5g)."""
    if not ranked_ids:
        return False
    return relevance.get(ranked_ids[0], 0) == 2


# ---------------------------------------------------------------------------------------------
# §5b: cluster-level bootstrap
# ---------------------------------------------------------------------------------------------


@dataclass
class FamilyMetricSample:
    """One topic_family_id's contribution to a bootstrap resample: the per-query metric values
    (already computed, e.g. via ndcg_at_10 above) for every query in that family, for ONE config.
    `values` may contain fewer entries than the family's total query count if some queries'
    metric was None (undefined) for this config -- those are simply absent, not zero."""

    family_id: str
    values: list[float] = field(default_factory=list)


def cluster_bootstrap_mean_ci(
    family_samples: list[FamilyMetricSample],
    n_resamples: int = 10000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """§5b "Per-config CI": resamples whole families with replacement, computes the metric's mean
    over all included queries' values each draw. Returns (point_estimate_mean, ci_low, ci_high)
    -- point estimate is the mean over the ORIGINAL (non-resampled) data; ci_low/ci_high are the
    2.5th/97.5th percentiles of the resampled means."""
    rng = random.Random(seed)
    all_values = [v for fam in family_samples for v in fam.values]
    point_estimate = statistics.fmean(all_values) if all_values else float("nan")

    families = family_samples
    n = len(families)
    if n == 0:
        return point_estimate, float("nan"), float("nan")

    resampled_means = []
    for _ in range(n_resamples):
        draw = [families[rng.randrange(n)] for _ in range(n)]
        pooled = [v for fam in draw for v in fam.values]
        resampled_means.append(statistics.fmean(pooled) if pooled else 0.0)

    resampled_means.sort()
    lo_idx = int(0.025 * n_resamples)
    hi_idx = int(0.975 * n_resamples) - 1
    hi_idx = min(hi_idx, n_resamples - 1)
    return point_estimate, resampled_means[lo_idx], resampled_means[hi_idx]


def cluster_bootstrap_delta_ci(
    family_samples_a: dict[str, list[float]],
    family_samples_b: dict[str, list[float]],
    n_resamples: int = 10000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """§5b "Paired-delta CI": family_samples_a/b are {family_id: [per-query metric values for
    that config]}, over the SAME set of families (a query missing from one config's dict for a
    given family, e.g. an undefined NDCG, simply isn't averaged in for that config that draw).
    Each resample draws family ids ONCE and uses that SAME draw for both config A and config B
    (paired), computing delta = mean(A) - mean(B) on the matched resampled query set. Returns
    (point_estimate_delta, ci_low, ci_high)."""
    family_ids = sorted(set(family_samples_a) | set(family_samples_b))
    rng = random.Random(seed)

    def _mean_over(families: list[str], samples: dict[str, list[float]]) -> float:
        pooled = [v for fid in families for v in samples.get(fid, [])]
        return statistics.fmean(pooled) if pooled else 0.0

    point_a = _mean_over(family_ids, family_samples_a)
    point_b = _mean_over(family_ids, family_samples_b)
    point_delta = point_a - point_b

    n = len(family_ids)
    if n == 0:
        return point_delta, float("nan"), float("nan")

    deltas = []
    for _ in range(n_resamples):
        draw = [family_ids[rng.randrange(n)] for _ in range(n)]
        mean_a = _mean_over(draw, family_samples_a)
        mean_b = _mean_over(draw, family_samples_b)
        deltas.append(mean_a - mean_b)

    deltas.sort()
    lo_idx = int(0.025 * n_resamples)
    hi_idx = int(0.975 * n_resamples) - 1
    hi_idx = min(hi_idx, n_resamples - 1)
    return point_delta, deltas[lo_idx], deltas[hi_idx]


def ci_includes_zero(ci_low: float, ci_high: float) -> bool:
    return ci_low <= 0.0 <= ci_high


# ---------------------------------------------------------------------------------------------
# §5d: McNemar continuity-corrected test + Holm adjustment
# ---------------------------------------------------------------------------------------------


def mcnemar_continuity_corrected(b: int, c: int) -> tuple[float, float]:
    """b = count where A hit and B missed (discordant, A-only); c = count where B hit and A
    missed (discordant, B-only). Returns (chi2_statistic, two_sided_p_value), df=1,
    continuity-corrected: statistic = (|b-c|-1)^2 / (b+c), per §5d. b+c==0 (no discordant pairs
    at all) returns (0.0, 1.0) -- no evidence of any difference."""
    if b + c == 0:
        return 0.0, 1.0
    statistic = ((abs(b - c) - 1) ** 2) / (b + c)
    try:
        from scipy import stats as scipy_stats

        p_value = float(scipy_stats.chi2.sf(statistic, df=1))
    except ModuleNotFoundError as exc:
        if exc.name != "scipy":
            raise
        # Chi-square(1) survival function has the closed form erfc(sqrt(x/2)); keeping this
        # fallback makes the evaluation statistics usable in the lightweight benchmark runner
        # without making SciPy a runtime dependency.
        p_value = math.erfc(math.sqrt(statistic / 2.0))
    return statistic, p_value


def holm_adjust(raw_pvalues: list[float]) -> list[float]:
    """§5d exact Holm-Bonferroni adjusted-p-value procedure, order-preserving (returned list is
    the same length/order as raw_pvalues -- adjustment travels with each comparison's own
    identity, not sorted order):
    1. sort ascending
    2. scale: s_(i) = (m - i + 1) * p_(i), 1-indexed rank i
    3. cumulative max in rank order
    4. cap at 1
    5. map back to original positions
    """
    m = len(raw_pvalues)
    if m == 0:
        return []
    indexed = sorted(range(m), key=lambda i: raw_pvalues[i])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, orig_idx in enumerate(indexed, start=1):
        scaled = (m - rank + 1) * raw_pvalues[orig_idx]
        running_max = max(running_max, scaled)
        adjusted[orig_idx] = min(1.0, running_max)
    return adjusted


# ---------------------------------------------------------------------------------------------
# §5h: WIN/LOSS/TIE counts
# ---------------------------------------------------------------------------------------------


def win_loss_tie_counts(
    contender_top1_hits: list[bool], target_top1_hits: list[bool]
) -> tuple[int, int, int]:
    """§5h exact definition, paired per query: WIN = contender hit AND target missed; LOSS =
    target hit AND contender missed; TIE = both agree (both hit or both missed). Returns
    (win, loss, tie) counts. These WIN/LOSS counts are exactly McNemar's b/c discordant cells."""
    win = loss = tie = 0
    for c_hit, t_hit in zip(contender_top1_hits, target_top1_hits):
        if c_hit and not t_hit:
            win += 1
        elif t_hit and not c_hit:
            loss += 1
        else:
            tie += 1
    return win, loss, tie


# ---------------------------------------------------------------------------------------------
# §5g: decision rule helpers
# ---------------------------------------------------------------------------------------------

NDCG_WIN_THRESHOLD = 0.04  # 4 percentage points, §5g
TIE_BREAK_TOLERANCE = 0.005  # 0.5 percentage points, §5g (Codex round-4 d_max form)
ALPHA = 0.05


def select_tie_break_candidates(
    qualifying_deltas: dict[str, float],
) -> list[str]:
    """§5g exact tie-break trigger (Codex round-4 fix): d_max = highest SIGNED delta among
    qualifying contenders; candidate set = every qualifier within TIE_BREAK_TOLERANCE of d_max.
    Returns the candidate config names, or a single-element list (no tie-break needed) if fewer
    than 2 qualify within tolerance. Empty input returns []."""
    if not qualifying_deltas:
        return []
    d_max = max(qualifying_deltas.values())
    return [name for name, d in qualifying_deltas.items() if d_max - d <= TIE_BREAK_TOLERANCE]
