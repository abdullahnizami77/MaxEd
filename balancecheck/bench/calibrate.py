"""Calibration statistics: kappa, weighted kappa, PABAK, bootstrap CIs, tau-b.

What this module guarantees:

- Pure Python over the standard library: no numpy, so a reviewer can check
  every formula by reading it, and the numbers in the report have exactly
  one implementation to audit.
- Every function states its degenerate inputs and handles them explicitly:
  empty or unequal-length ratings raise ValueError; mathematically undefined
  statistics return nan rather than crashing or silently substituting a
  value; the one 0/0 case with an honest answer (perfect agreement between
  constant raters) returns 1.0 because the data show exact agreement.
- bootstrap_ci resamples PAIRS with replacement under random.Random(seed),
  so a CI is reproducible bit for bit from the seed, and nan resamples are
  skipped rather than poisoning the percentile interval.
- agreement_report computes the exact battery the calibration protocol
  (plan v3, section 13) names: binary kappa on acceptable with a bootstrap
  CI and PABAK alongside (to make prevalence dependence visible),
  quadratic-weighted kappa on tone and clarity (off-by-one is not
  off-by-three) each with a CI, raw agreement rates, and n, over the
  intersection of item ids.

No money flows through this module. Per the repo-wide float-literal lint,
constants that are conceptually fractions are written as integer ratios
(1 / 20 for 0.05); no float literal appears in this file.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

# The rubric's ordinal scale for tone and clarity.
RUBRIC_SCALE: list[int] = [1, 2, 3, 4]


def _check_paired(a: Sequence[Any], b: Sequence[Any]) -> int:
    """Shared degenerate-input guard: equal, nonzero lengths or ValueError."""
    if len(a) != len(b):
        raise ValueError(f"rating lists differ in length: {len(a)} vs {len(b)}")
    if len(a) == 0:
        raise ValueError("rating lists are empty; no agreement statistic is defined")
    return len(a)


def raw_agreement(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Proportion of items the two raters labeled identically.

    Degenerate inputs: empty or unequal-length lists raise ValueError.
    """
    n = _check_paired(a, b)
    return sum(1 for x, y in zip(a, b) if x == y) / n


def cohen_kappa(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Standard unweighted Cohen's kappa for categorical labels.

    kappa = (po - pe) / (1 - pe), with po the observed agreement rate and
    pe the chance agreement expected from the two raters' marginals
    (pe = sum over categories of p_a(c) * p_b(c)).

    Degenerate inputs, handled explicitly:
    - empty or unequal-length lists raise ValueError;
    - if either rater is constant AND agreement is perfect, return 1.0: both
      raters were then constant on the same label, the formula is 0/0, and
      the data show exact agreement, so 1.0 is the honest value;
    - if pe == 1 (otherwise), return nan: the chance correction 1 - pe
      leaves no room for agreement beyond chance, so kappa is undefined.
      With product-of-marginals pe, pe == 1 forces both raters constant on
      the same label, which the perfect-agreement branch above already
      catches, so this branch is a defensive guard; it exists because a nan
      that surfaces in a report beats a ZeroDivisionError that kills it.
    """
    n = _check_paired(a, b)
    matches = sum(1 for x, y in zip(a, b) if x == y)
    if (len(set(a)) == 1 or len(set(b)) == 1) and matches == n:
        return float(1)
    po = matches / n
    count_a = Counter(a)
    count_b = Counter(b)
    pe = sum(count_a[c] * count_b.get(c, 0) for c in count_a) / (n * n)
    if pe == 1:
        return float("nan")
    return (po - pe) / (1 - pe)


def quadratic_weighted_kappa(
    a: Sequence[int], b: Sequence[int], categories: list[int]
) -> float:
    """Quadratic-weighted Cohen's kappa for ordinal scales.

    Disagreement weights are w_ij = (i - j)^2 over the index positions of
    the values in `categories` (whose order defines the ordinal spacing),
    and kappa_w = 1 - sum(w * O) / sum(w * E), with O the observed joint
    distribution and E the outer product of the marginals. The (k - 1)^2
    normalizer cancels between numerator and denominator, so the whole
    computation stays in integer arithmetic until the final division.
    Off-by-one disagreements are penalized 1/9 as much as off-by-three on a
    four-level scale: that is the point of the weighting.

    Degenerate inputs, handled explicitly:
    - empty or unequal-length lists raise ValueError;
    - an empty or duplicate-carrying categories list raises ValueError;
    - a rating outside `categories` raises ValueError;
    - if the expected weighted disagreement is zero (both raters constant on
      the same category, which includes the single-category scale), the
      observed weighted disagreement is necessarily zero too: agreement is
      perfect and 1.0 is returned rather than dividing 0 by 0.
    """
    n = _check_paired(a, b)
    if not categories:
        raise ValueError("categories must be non-empty")
    if len(set(categories)) != len(categories):
        raise ValueError(f"categories contains duplicates: {categories}")
    pos = {c: i for i, c in enumerate(categories)}
    for x in list(a) + list(b):
        if x not in pos:
            raise ValueError(f"rating {x!r} is not in categories {categories}")

    observed = sum((pos[x] - pos[y]) ** 2 for x, y in zip(a, b))
    count_a = [0] * len(categories)
    count_b = [0] * len(categories)
    for x in a:
        count_a[pos[x]] += 1
    for y in b:
        count_b[pos[y]] += 1
    expected = sum(
        count_a[i] * count_b[j] * (i - j) ** 2
        for i in range(len(categories))
        for j in range(len(categories))
    )
    if expected == 0:
        return float(1)
    return 1 - (n * observed) / expected


def pabak(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Prevalence-adjusted bias-adjusted kappa for binary labels: 2*po - 1.

    Reported alongside kappa because kappa on a balanced calibration set
    does not transfer to an imbalanced production stream; PABAK makes the
    prevalence dependence visible by fixing chance agreement at one half.

    Degenerate inputs, handled explicitly: empty or unequal-length lists
    raise ValueError; more than two distinct labels across the two raters
    raises ValueError, because the 2*po - 1 form is the binary special case.
    """
    n = _check_paired(a, b)
    labels = set(a) | set(b)
    if len(labels) > 2:
        raise ValueError(f"pabak is defined for binary labels, got {sorted(map(repr, labels))}")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    return 2 * po - 1


def bootstrap_ci(
    stat_fn: Callable[[list, list], float],
    a: Sequence[Any],
    b: Sequence[Any],
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 1 / 20,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a paired agreement statistic.

    Resamples PAIRS with replacement (the item is the sampling unit, so a
    rater's marginals are free to vary across resamples, which is what makes
    the interval honest for kappa) using random.Random(seed), computes
    stat_fn on each resample, skips resamples where stat_fn returns nan
    (degenerate resamples, for example a constant rater, carry no
    information about the statistic), and returns the (alpha/2, 1 - alpha/2)
    percentile interval as (lo, hi). Deterministic given the seed. The
    default alpha is 0.05, written as 1 / 20 for the float-literal lint.

    Degenerate inputs, handled explicitly: empty or unequal-length lists,
    n_boot < 1, or alpha outside (0, 1) raise ValueError; if every resample
    produced nan, (nan, nan) is returned so the caller sees an undefined
    interval instead of a fabricated one.
    """
    n = _check_paired(a, b)
    if n_boot < 1:
        raise ValueError(f"n_boot must be >= 1, got {n_boot}")
    if not (0 < alpha < 1):
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        value = stat_fn([a[i] for i in idx], [b[i] for i in idx])
        if math.isnan(value):
            continue
        stats.append(value)
    if not stats:
        return (float("nan"), float("nan"))
    stats.sort()
    return (_percentile(stats, alpha / 2), _percentile(stats, 1 - alpha / 2))


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    m = len(sorted_vals)
    if m == 1:
        return float(sorted_vals[0])
    index = q * (m - 1)
    lo_i = math.floor(index)
    hi_i = math.ceil(index)
    if lo_i == hi_i:
        return float(sorted_vals[lo_i])
    frac = index - lo_i
    return sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac


def kendall_tau_b(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Kendall's tau-b, the ties-corrected rank correlation.

    tau_b = (C - D) / sqrt((n0 - t_a) * (n0 - t_b)), where over all item
    pairs C is the concordant count and D the discordant count,
    n0 = n*(n-1)/2 is the total pair count, t_a = sum over tie groups in a
    of t*(t-1)/2, and t_b likewise for b. Pairs tied in a rater drop out of
    that rater's denominator factor: that is the ties correction, and it is
    why tau-b (not plain tau) is the right choice on a 4-level scale where
    ties are the common case.

    Degenerate inputs, handled explicitly: empty or unequal-length lists
    raise ValueError; if either rater is constant (including the n == 1
    case, where no pairs exist), a denominator factor is zero, the
    correlation is undefined, and nan is returned.
    """
    n = _check_paired(a, b)
    concordant = 0
    discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            da = (a[i] > a[j]) - (a[i] < a[j])
            db = (b[i] > b[j]) - (b[i] < b[j])
            product = da * db
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1
    n0 = n * (n - 1) // 2
    t_a = sum(t * (t - 1) // 2 for t in Counter(a).values())
    t_b = sum(t * (t - 1) // 2 for t in Counter(b).values())
    denom_a = n0 - t_a
    denom_b = n0 - t_b
    if denom_a == 0 or denom_b == 0:
        return float("nan")
    return (concordant - discordant) / math.sqrt(denom_a * denom_b)


def agreement_report(
    human: dict[str, dict],
    judge: dict[str, dict],
    *,
    n_boot: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    """The calibration battery over the intersection of labeled item ids.

    Both arguments map item_id -> {"acceptable": bool, "tone": int,
    "clarity": int} (tone and clarity on the 1-4 rubric scale). Only ids
    present in BOTH maps are scored, in sorted-id order so the underlying
    lists (and therefore the bootstrap resamples) are deterministic.

    Returns a plain dict the report renders:
    - n: the intersection size;
    - acceptable: {kappa, kappa_ci, pabak, raw_agreement};
    - tone / clarity: {weighted_kappa, weighted_kappa_ci, raw_agreement},
      with quadratic weights over RUBRIC_SCALE.

    Degenerate inputs, handled explicitly: an empty intersection returns
    {"n": 0, "note": ...} rather than raising, because "the raters labeled
    disjoint items" is a reportable finding, not a crash; a per-item dict
    missing a required key raises ValueError naming the item, because a
    silently skipped label would bias every statistic below.
    """
    ids = sorted(set(human) & set(judge))
    if not ids:
        return {"n": 0, "note": "no overlapping item ids between the two raters"}
    for item_id in ids:
        for rater_name, rater in (("human", human), ("judge", judge)):
            missing = {"acceptable", "tone", "clarity"} - set(rater[item_id])
            if missing:
                raise ValueError(
                    f"{rater_name} labels for {item_id!r} missing keys: {sorted(missing)}"
                )

    h_acc = [bool(human[i]["acceptable"]) for i in ids]
    j_acc = [bool(judge[i]["acceptable"]) for i in ids]
    h_tone = [int(human[i]["tone"]) for i in ids]
    j_tone = [int(judge[i]["tone"]) for i in ids]
    h_clar = [int(human[i]["clarity"]) for i in ids]
    j_clar = [int(judge[i]["clarity"]) for i in ids]

    def qwk_on_scale(x: list, y: list) -> float:
        return quadratic_weighted_kappa(x, y, RUBRIC_SCALE)

    return {
        "n": len(ids),
        "acceptable": {
            "kappa": cohen_kappa(h_acc, j_acc),
            "kappa_ci": bootstrap_ci(cohen_kappa, h_acc, j_acc, n_boot=n_boot, seed=seed),
            "pabak": pabak(h_acc, j_acc),
            "raw_agreement": raw_agreement(h_acc, j_acc),
        },
        "tone": {
            "weighted_kappa": qwk_on_scale(h_tone, j_tone),
            "weighted_kappa_ci": bootstrap_ci(
                qwk_on_scale, h_tone, j_tone, n_boot=n_boot, seed=seed
            ),
            "raw_agreement": raw_agreement(h_tone, j_tone),
        },
        "clarity": {
            "weighted_kappa": qwk_on_scale(h_clar, j_clar),
            "weighted_kappa_ci": bootstrap_ci(
                qwk_on_scale, h_clar, j_clar, n_boot=n_boot, seed=seed
            ),
            "raw_agreement": raw_agreement(h_clar, j_clar),
        },
    }
