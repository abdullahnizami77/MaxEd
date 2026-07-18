"""Hand-computed calibration statistics: every number asserted here was
worked out on paper first, so a formula bug in bench/calibrate.py fails
against arithmetic, not against another implementation.

Hermetic: pure functions, no fixtures, no I/O, no model. Per the repo-wide
float-literal lint, expected fractions are written as integer ratios
(2 / 5 for 0.4, 1 / 10**9 for the tolerance).
"""

from __future__ import annotations

import math

import pytest

from balancecheck.bench.calibrate import (
    RUBRIC_SCALE,
    agreement_report,
    bootstrap_ci,
    cohen_kappa,
    kendall_tau_b,
    pabak,
    quadratic_weighted_kappa,
    raw_agreement,
)

TOL = 1 / 10**9


def _po7_pe5_lists() -> tuple[list[int], list[int]]:
    """20 binary items with po = 0.7 and balanced marginals, so pe = 0.5.

    7 pairs (0,0), 7 pairs (1,1), 3 pairs (0,1), 3 pairs (1,0): each rater
    marks exactly 10 zeros and 10 ones, agreement is 14/20 = 0.7, chance is
    0.5*0.5 + 0.5*0.5 = 0.5, and kappa = (0.7 - 0.5) / (1 - 0.5) = 0.4.
    """
    a = [0] * 7 + [1] * 7 + [0] * 3 + [1] * 3
    b = [0] * 7 + [1] * 7 + [1] * 3 + [0] * 3
    return a, b


# ---------------------------------------------------------------------------
# cohen_kappa
# ---------------------------------------------------------------------------


def test_kappa_hand_computed_po7_pe5() -> None:
    a, b = _po7_pe5_lists()
    assert abs(cohen_kappa(a, b) - 2 / 5) < TOL


def test_kappa_perfect_agreement_is_one() -> None:
    assert cohen_kappa([1, 0, 2, 1, 0], [1, 0, 2, 1, 0]) == 1


def test_kappa_constant_raters_in_perfect_agreement_is_one() -> None:
    # The formula is 0/0 here (pe == 1); the documented degenerate rule
    # returns 1.0 because the data show exact agreement.
    assert cohen_kappa([1, 1, 1], [1, 1, 1]) == 1
    assert cohen_kappa([True] * 4, [True] * 4) == 1


def test_kappa_one_constant_rater_imperfect_agreement_is_zero() -> None:
    # a is constant, b disagrees once: po = 3/4 and pe = 1*(3/4) = 3/4, so
    # kappa = 0: a constant rater shows no agreement beyond its marginal.
    assert abs(cohen_kappa([1, 1, 1, 1], [1, 1, 1, 0])) < TOL


def test_kappa_constant_raters_on_different_labels_is_zero() -> None:
    # po = 0 and pe = 0 (the marginals never overlap), so kappa = 0.
    assert abs(cohen_kappa([0, 0, 0], [1, 1, 1])) < TOL


def test_kappa_rejects_degenerate_lists() -> None:
    with pytest.raises(ValueError):
        cohen_kappa([], [])
    with pytest.raises(ValueError):
        cohen_kappa([1, 0], [1])


# ---------------------------------------------------------------------------
# quadratic_weighted_kappa
# ---------------------------------------------------------------------------


def test_weighted_kappa_perfect_agreement_is_one() -> None:
    assert quadratic_weighted_kappa([1, 2, 3, 4], [1, 2, 3, 4], RUBRIC_SCALE) == 1


def test_weighted_kappa_all_off_by_one_hand_computed() -> None:
    # a = [1,2,3,4], b = [2,1,4,3]: every item off by one. Observed weighted
    # disagreement: 4 pairs * 1^2 = 4, times n gives 16. Expected (uniform
    # marginals): sum over all 16 (i,j) cells of (i-j)^2 = 40. So
    # kappa_w = 1 - 16/40 = 3/5.
    value = quadratic_weighted_kappa([1, 2, 3, 4], [2, 1, 4, 3], RUBRIC_SCALE)
    assert abs(value - 3 / 5) < TOL
    assert 0 < value < 1


def test_weighted_kappa_off_by_one_beats_off_by_three() -> None:
    a = [1, 2, 3, 4]
    off_by_one = quadratic_weighted_kappa(a, [1, 2, 3, 3], RUBRIC_SCALE)
    off_by_three = quadratic_weighted_kappa(a, [1, 2, 3, 1], RUBRIC_SCALE)
    assert off_by_one > off_by_three
    assert off_by_one < 1


def test_weighted_kappa_constant_raters_same_category_is_one() -> None:
    assert quadratic_weighted_kappa([2, 2, 2], [2, 2, 2], RUBRIC_SCALE) == 1


def test_weighted_kappa_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        quadratic_weighted_kappa([], [], RUBRIC_SCALE)
    with pytest.raises(ValueError):
        quadratic_weighted_kappa([1, 5], [1, 2], RUBRIC_SCALE)
    with pytest.raises(ValueError):
        quadratic_weighted_kappa([1], [1], [])
    with pytest.raises(ValueError):
        quadratic_weighted_kappa([1], [1], [1, 1, 2])


# ---------------------------------------------------------------------------
# pabak
# ---------------------------------------------------------------------------


def test_pabak_hand_computed() -> None:
    a, b = _po7_pe5_lists()
    # po = 0.7, so PABAK = 2*0.7 - 1 = 0.4 (equal to kappa here only because
    # the set is perfectly balanced; that coincidence is the point of
    # reporting both).
    assert abs(pabak(a, b) - 2 / 5) < TOL


def test_pabak_bounds() -> None:
    assert pabak([1, 0, 1], [1, 0, 1]) == 1
    assert pabak([1, 1, 0], [0, 0, 1]) == -1


def test_pabak_rejects_non_binary() -> None:
    with pytest.raises(ValueError):
        pabak([1, 2, 3], [1, 2, 3])
    with pytest.raises(ValueError):
        pabak([], [])


# ---------------------------------------------------------------------------
# bootstrap_ci
# ---------------------------------------------------------------------------


def test_bootstrap_ci_deterministic_for_a_fixed_seed() -> None:
    a, b = _po7_pe5_lists()
    first = bootstrap_ci(cohen_kappa, a, b, n_boot=300, seed=7)
    second = bootstrap_ci(cohen_kappa, a, b, n_boot=300, seed=7)
    assert first == second


def test_bootstrap_ci_contains_the_point_estimate() -> None:
    a, b = _po7_pe5_lists()
    lo, hi = bootstrap_ci(cohen_kappa, a, b, n_boot=500, seed=0)
    assert lo <= 2 / 5 <= hi
    assert lo < hi


def test_bootstrap_ci_all_nan_resamples_returns_nan_interval() -> None:
    def always_nan(_x: list, _y: list) -> float:
        return float("nan")

    lo, hi = bootstrap_ci(always_nan, [1, 0], [0, 1], n_boot=50, seed=0)
    assert math.isnan(lo) and math.isnan(hi)


def test_bootstrap_ci_rejects_degenerate_inputs() -> None:
    with pytest.raises(ValueError):
        bootstrap_ci(cohen_kappa, [], [], n_boot=10, seed=0)
    with pytest.raises(ValueError):
        bootstrap_ci(cohen_kappa, [1, 0], [0, 1], n_boot=0, seed=0)
    with pytest.raises(ValueError):
        bootstrap_ci(cohen_kappa, [1, 0], [0, 1], n_boot=10, seed=0, alpha=2)


# ---------------------------------------------------------------------------
# kendall_tau_b
# ---------------------------------------------------------------------------


def test_kendall_tau_b_perfect_and_reversed() -> None:
    assert kendall_tau_b([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == 1
    assert kendall_tau_b([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == -1


def test_kendall_tau_b_hand_checked_no_ties() -> None:
    # Pairs of [1,2,3] vs [1,3,2]: (1,2) and (1,3) concordant, (2,3)
    # discordant: tau = (2 - 1) / 3 = 1/3.
    assert abs(kendall_tau_b([1, 2, 3], [1, 3, 2]) - 1 / 3) < TOL


def test_kendall_tau_b_hand_checked_with_ties() -> None:
    # a = [1,1,2], b = [1,2,3]: the (0,1) pair is tied in a, the other two
    # pairs are concordant. n0 = 3, t_a = 1, t_b = 0, so
    # tau_b = 2 / sqrt(2 * 3) = 2 / sqrt(6).
    expected = 2 / math.sqrt(6)
    assert abs(kendall_tau_b([1, 1, 2], [1, 2, 3]) - expected) < TOL


def test_kendall_tau_b_constant_rater_is_nan() -> None:
    assert math.isnan(kendall_tau_b([1, 1, 1], [1, 2, 3]))
    assert math.isnan(kendall_tau_b([1], [1]))


def test_kendall_tau_b_rejects_degenerate_lists() -> None:
    with pytest.raises(ValueError):
        kendall_tau_b([], [])


# ---------------------------------------------------------------------------
# agreement_report
# ---------------------------------------------------------------------------


def _labels(acceptable: list[bool], tone: list[int], clarity: list[int]) -> dict[str, dict]:
    return {
        f"item-{i}": {"acceptable": acc, "tone": t, "clarity": c}
        for i, (acc, t, c) in enumerate(zip(acceptable, tone, clarity))
    }


def test_agreement_report_matches_direct_computation() -> None:
    human = _labels(
        [True, True, False, False, True, False],
        [3, 4, 2, 1, 3, 2],
        [3, 3, 2, 2, 4, 1],
    )
    judge = _labels(
        [True, False, False, True, True, False],
        [4, 3, 2, 2, 3, 1],
        [3, 3, 1, 2, 4, 2],
    )
    # Extra non-overlapping ids on each side must be ignored.
    human["only-human"] = {"acceptable": True, "tone": 3, "clarity": 3}
    judge["only-judge"] = {"acceptable": False, "tone": 1, "clarity": 1}

    report = agreement_report(human, judge, n_boot=200, seed=0)
    assert report["n"] == 6

    ids = sorted(set(human) & set(judge))
    h_acc = [human[i]["acceptable"] for i in ids]
    j_acc = [judge[i]["acceptable"] for i in ids]
    h_tone = [human[i]["tone"] for i in ids]
    j_tone = [judge[i]["tone"] for i in ids]
    h_clar = [human[i]["clarity"] for i in ids]
    j_clar = [judge[i]["clarity"] for i in ids]

    acc = report["acceptable"]
    assert acc["kappa"] == cohen_kappa(h_acc, j_acc)
    assert acc["pabak"] == pabak(h_acc, j_acc)
    assert acc["raw_agreement"] == raw_agreement(h_acc, j_acc)
    lo, hi = acc["kappa_ci"]
    assert lo <= hi

    tone = report["tone"]
    assert tone["weighted_kappa"] == quadratic_weighted_kappa(h_tone, j_tone, RUBRIC_SCALE)
    assert tone["raw_agreement"] == raw_agreement(h_tone, j_tone)
    t_lo, t_hi = tone["weighted_kappa_ci"]
    assert t_lo <= t_hi

    clarity = report["clarity"]
    assert clarity["weighted_kappa"] == quadratic_weighted_kappa(h_clar, j_clar, RUBRIC_SCALE)
    assert clarity["raw_agreement"] == raw_agreement(h_clar, j_clar)


def test_agreement_report_is_deterministic() -> None:
    human = _labels([True, False, True, True], [3, 2, 4, 3], [3, 3, 4, 2])
    judge = _labels([True, False, False, True], [3, 2, 3, 4], [3, 2, 4, 2])
    first = agreement_report(human, judge, n_boot=100, seed=3)
    second = agreement_report(human, judge, n_boot=100, seed=3)
    assert first == second


def test_agreement_report_empty_intersection() -> None:
    report = agreement_report(
        {"a": {"acceptable": True, "tone": 3, "clarity": 3}},
        {"b": {"acceptable": True, "tone": 3, "clarity": 3}},
    )
    assert report["n"] == 0
    assert "note" in report


def test_agreement_report_missing_key_raises() -> None:
    human = {"x": {"acceptable": True, "tone": 3, "clarity": 3}}
    judge = {"x": {"acceptable": True, "tone": 3}}
    with pytest.raises(ValueError):
        agreement_report(human, judge)
