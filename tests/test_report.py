"""The report generator: deterministic output, honest placeholders, and the
README drift mechanism (invariant I10's engine).

Every event is constructed inline and appended to a pytest tmp_path log;
raw result JSON files are written into tmp_path; no network, no model, no
credentials. write_all twice into two directories must be byte-identical;
readme_inject must replace block interiors, be idempotent, and revert a
hand-edited number inside a block; an unknown block name must raise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from balancecheck.config import Config
from balancecheck.contracts.models import (
    CapabilityGapEvent,
    GapCategory,
    ScoreEvent,
    TraceEvent,
)
from balancecheck.spine.events import append_event
from balancecheck.spine.report import (
    before_after_table,
    calibration_table,
    capability_gaps_table,
    injected_errors_table,
    pairwise_table,
    readme_inject,
    trace_stats,
    write_all,
)

# Raw JSON literals are strings, not Python structures, so no float literal
# appears in this module even where the raw files carry kappa floats.
CALIBRATION_JSON = (
    '{"binary_kappa": 0.62, "binary_kappa_ci": [0.18, 0.9], "pabak": 0.5,'
    ' "raw_agreement": 0.81, "tone_weighted_kappa": 0.44, "n": 16,'
    ' "labeled_at": "2026-07-17T00:00:00Z", "judged_at": "2026-07-18T00:00:00Z"}'
)


def _score(
    scenario_id: str,
    pass_label: str,
    grounding: tuple[int, int],
    completeness: tuple[int, int],
    revisions: int,
    terminal: str,
    stage: str = "first_pass",
) -> ScoreEvent:
    return ScoreEvent(
        gen_id=f"G-{pass_label}-{scenario_id}",
        scenario_id=scenario_id,
        pass_label=pass_label,
        stage=stage,  # type: ignore[arg-type]
        grounding_checked=grounding[0],
        grounding_total=grounding[1],
        completeness_present=completeness[0],
        completeness_total=completeness[1],
        revision_count=revisions,
        terminal_action=terminal,
    )


def _trace(task: str, ok: bool, error: str = "") -> TraceEvent:
    return TraceEvent(
        task=task,
        prompt_sha="ab12",
        prompt="p",
        output="o",
        model_name="stub",
        attempt=1,
        ok=ok,
        error=error,
    )


def _gap(gen_id: str, category: GapCategory) -> CapabilityGapEvent:
    return CapabilityGapEvent(
        gen_id=gen_id,
        scenario_id="S-B-03",
        missing=category,
        detail="d",
        would_need="w",
        resolvable_by="human",
    )


@pytest.fixture()
def populated(tmp_path: Path) -> tuple[Config, Path]:
    """A log with score/gap/trace events plus raw JSON files, all inline."""
    log = tmp_path / "log" / "events.jsonl"
    cfg = Config(mode="stub", log_path=log, stub_file=tmp_path / "stub.json")

    events = [
        _score("S-B-01", "pass1", (3, 5), (2, 4), 2, "human_gate"),
        _score("S-B-02", "pass1", (2, 4), (3, 4), 1, "human_gate"),
        _score("S-B-03", "pass1", (1, 3), (1, 2), 2, "escalate"),
        _score("S-B-01", "pass2", (5, 5), (4, 4), 0, "human_gate"),
        _score("S-B-02", "pass2", (3, 4), (3, 4), 1, "human_gate"),
        _score("S-B-03", "pass2", (2, 3), (2, 2), 1, "abstain"),
        # A final-stage score that must NOT enter the before/after table.
        _score("S-B-01", "pass1", (5, 5), (4, 4), 2, "human_gate", stage="final"),
        _gap("G-1", GapCategory.ALLOCATION_REFERENCE),
        _gap("G-3", GapCategory.ALLOCATION_REFERENCE),
        _gap("G-2", GapCategory.DOCUMENT_ABSENT),
        _trace("draft", True),
        _trace("draft", True),
        _trace("judge", False, error="structured parse failure: not JSON"),
        _trace("judge", True),
    ]
    for e in events:
        append_event(e, log)

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "injected_errors.json").write_text(
        json.dumps(
            [
                {
                    "error_id": "E-CLEAN-S-A-01",
                    "tier": 0,
                    "scenario_id": "S-A-01",
                    "truth": "clean",
                    "expected_kind": "none",
                    "expected_action": "human_gate",
                    "decision_action": "human_gate",
                    "decision_reason": "all checks passed",
                    "caught": False,
                },
                {
                    "error_id": "E-T1-01-S-A-01",
                    "tier": 1,
                    "scenario_id": "S-A-01",
                    "truth": "corrupted",
                    "expected_kind": "amount_swap",
                    "expected_action": "revise_or_escalate",
                    "decision_action": "revise",
                    "decision_reason": "c_amt_fail",
                    "caught": True,
                },
                {
                    "error_id": "E-T1-02-S-A-01",
                    "tier": 1,
                    "scenario_id": "S-A-01",
                    "truth": "corrupted",
                    "expected_kind": "fabricated_id",
                    "expected_action": "escalate",
                    "decision_action": "human_gate",
                    "decision_reason": "all checks passed",
                    "caught": False,
                    "notes": "missed",
                },
                {
                    "error_id": "E-T3-01",
                    "tier": 3,
                    "scenario_id": "S-A-06",
                    "truth": "ambiguous",
                    "expected_kind": "ambiguous_allocation",
                    "expected_action": "abstain_or_escalate",
                    "decision_action": "abstain",
                    "decision_reason": "ambiguous allocation",
                    "caught": True,
                },
            ],
            indent=2,
        )
        + "\n"
    )
    (raw / "calibration.json").write_text(CALIBRATION_JSON + "\n")
    (raw / "pairwise.json").write_text(
        json.dumps(
            [
                {"scenario_id": "S-B-01", "verdict": "b", "flipped": False},
                {"scenario_id": "S-B-02", "verdict": "tie", "flipped": True},
                {"scenario_id": "S-B-03", "verdict": "a", "flipped": False},
            ],
            indent=2,
        )
        + "\n"
    )
    return cfg, raw


# ---------------------------------------------------------------------------
# determinism: write_all twice is byte-identical (the engine behind I10)
# ---------------------------------------------------------------------------


def test_write_all_is_deterministic(populated, tmp_path: Path) -> None:
    cfg, raw = populated
    dir1, dir2 = tmp_path / "r1", tmp_path / "r2"
    written1 = write_all(cfg, dir1, raw)
    written2 = write_all(cfg, dir2, raw)
    assert set(written1) == set(written2)
    names = sorted(p.name for p in dir1.glob("*.md"))
    assert names == [
        "before_after.md",
        "calibration.md",
        "capability_gaps.md",
        "injected_errors.md",
        "oracle_crosscheck.md",
        "pairwise.md",
        "trace_stats.md",
    ]
    for name in names:
        assert (dir1 / name).read_bytes() == (dir2 / name).read_bytes(), name


# ---------------------------------------------------------------------------
# individual tables
# ---------------------------------------------------------------------------


def test_before_after_aggregates_and_pairing(populated) -> None:
    cfg, _raw = populated
    from balancecheck.spine.events import read_events

    events = read_events(cfg.log_path)
    md = before_after_table(events)
    # pass1 pooled: grounding 6/12, completeness 6/10, revisions 5/3; the
    # final-stage 5/5 row for S-B-01 must not have leaked in.
    assert "| pass1 | 3 | 6/12 (50%) | 6/10 (60%) | 5/3 |" in md
    assert "| pass2 | 3 | 10/12 (83%) | 9/10 (90%) | 2/3 |" in md
    assert "human_gate: 2, escalate: 1" in md
    # paired rows with plain +n/-n/0 delta markers
    assert "| S-B-01 | 3/5 (60%) -> 5/5 (100%) | +40 |" in md
    assert "| 2 -> 0 | -2 |" in md
    assert "| 1 -> 1 | 0 |" in md
    # the honesty sentence, verbatim, inside the generated markdown
    assert "n = 3 paired scenarios." in md
    assert "n is too small for a significance claim; deltas are directional." in md


def test_before_after_empty_log(tmp_path: Path) -> None:
    md = before_after_table([])
    assert "(no first-pass score events recorded)" in md


def test_injected_errors_tiers_and_balanced_accuracy(populated) -> None:
    _cfg, raw = populated
    md = injected_errors_table(raw)
    assert "## Tier 0" in md and "## Tier 1" in md and "## Tier 3" in md
    assert "Tier 1: caught 1/2 (50%)." in md
    assert "Tier 3: caught 1/1 (100%)." in md
    # balanced accuracy: tier 1 sens 50%, clean-pass 100% -> 75%
    assert "Tier 1: balanced accuracy 75%" in md
    assert "Tier 3: balanced accuracy 100%" in md
    assert "missed" in md  # notes column carried through


def test_injected_errors_placeholder(tmp_path: Path) -> None:
    assert "(errors run not yet recorded)" in injected_errors_table(tmp_path)


def test_capability_gaps_sorted_by_count_desc(populated) -> None:
    cfg, _raw = populated
    from balancecheck.spine.events import read_events

    md = capability_gaps_table(read_events(cfg.log_path))
    assert "| allocation_reference | 2 | G-1, G-3 |" in md
    assert "| document_absent | 1 | G-2 |" in md
    assert md.index("allocation_reference") < md.index("document_absent")
    assert "Total capability gaps: 3." in md


def test_calibration_table_renders_fields_and_caveat(populated) -> None:
    _cfg, raw = populated
    md = calibration_table(raw)
    assert "| binary_kappa | 0.62 |" in md
    assert "| binary_kappa_ci | [0.18, 0.9] |" in md
    assert "| pabak | 0.5 |" in md
    assert "| tone_weighted_kappa | 0.44 |" in md
    assert "| raw_agreement | 0.81 |" in md
    assert (
        "Kappa on n=16 carries a wide CI and is a calibration signal, not a certification."
        in md.replace("\n", " ")
    )


def test_calibration_placeholder(tmp_path: Path) -> None:
    assert "(calibration not yet recorded)" in calibration_table(tmp_path)


def test_pairwise_counts_and_flip_label(populated) -> None:
    _cfg, raw = populated
    md = pairwise_table(raw)
    assert "| pass2 wins (verdict b) | 1 |" in md
    assert "| ties | 1 |" in md
    assert "| pass1 wins (verdict a) | 1 |" in md
    assert "Decisive pairs: 2/3 (66%)." in md
    assert "inconsistency rate (an upper bound on position bias): 1/3 (33%)" in md


def test_pairwise_placeholder(tmp_path: Path) -> None:
    assert "(pairwise run not yet recorded)" in pairwise_table(tmp_path)


def test_trace_stats_counts(populated) -> None:
    cfg, _raw = populated
    from balancecheck.spine.events import read_events

    md = trace_stats(read_events(cfg.log_path))
    assert "| draft | 2 | 2 | 100% |" in md
    assert "| judge | 2 | 1 | 50% |" in md
    assert "Structured-parse failures: 1" in md
    assert "Total trace lines: 4" in md


# ---------------------------------------------------------------------------
# readme injection: the drift mechanism
# ---------------------------------------------------------------------------


README_TEMPLATE = (
    "# BALANCECHECK\n"
    "\n"
    "intro prose that must survive injection untouched.\n"
    "\n"
    "<!-- BC:BEGIN before_after -->\n"
    "stale hand-typed numbers\n"
    "<!-- BC:END before_after -->\n"
    "\n"
    "middle prose.\n"
    "\n"
    "<!-- BC:BEGIN trace_stats -->\n"
    "<!-- BC:END trace_stats -->\n"
    "\n"
    "tail prose.\n"
)


def test_readme_inject_replaces_block_interiors(populated, tmp_path: Path) -> None:
    cfg, raw = populated
    results = tmp_path / "results"
    write_all(cfg, results, raw)
    readme = tmp_path / "README.md"
    readme.write_text(README_TEMPLATE, encoding="utf-8")
    new_text = readme_inject(readme, results)
    assert "stale hand-typed numbers" not in new_text
    assert "| pass1 | 3 | 6/12 (50%) | 6/10 (60%) | 5/3 |" in new_text
    assert "Total trace lines: 4" in new_text
    # prose outside the blocks is untouched
    for prose in ("intro prose", "middle prose.", "tail prose."):
        assert prose in new_text
    # markers survive so the injection can run again
    assert "<!-- BC:BEGIN before_after -->" in new_text
    assert "<!-- BC:END trace_stats -->" in new_text


def test_readme_inject_is_idempotent(populated, tmp_path: Path) -> None:
    cfg, raw = populated
    results = tmp_path / "results"
    write_all(cfg, results, raw)
    readme = tmp_path / "README.md"
    readme.write_text(README_TEMPLATE, encoding="utf-8")
    once = readme_inject(readme, results)
    readme.write_text(once, encoding="utf-8")
    twice = readme_inject(readme, results)
    assert twice == once


def test_readme_inject_reverts_hand_edited_number(populated, tmp_path: Path) -> None:
    """The drift mechanism: a number edited by hand inside a block is
    reverted by re-injection, which is what the I10 drift test detects."""
    cfg, raw = populated
    results = tmp_path / "results"
    write_all(cfg, results, raw)
    readme = tmp_path / "README.md"
    readme.write_text(README_TEMPLATE, encoding="utf-8")
    clean = readme_inject(readme, results)
    tampered = clean.replace("6/12 (50%)", "11/12 (91%)")
    assert tampered != clean
    readme.write_text(tampered, encoding="utf-8")
    reverted = readme_inject(readme, results)
    assert reverted == clean
    assert "11/12 (91%)" not in reverted


def test_readme_inject_unknown_block_raises(populated, tmp_path: Path) -> None:
    cfg, raw = populated
    results = tmp_path / "results"
    write_all(cfg, results, raw)
    readme = tmp_path / "README.md"
    readme.write_text(
        "<!-- BC:BEGIN not_a_real_table -->\nx\n<!-- BC:END not_a_real_table -->\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown README injection block"):
        readme_inject(readme, results)


def test_readme_inject_unterminated_block_raises(populated, tmp_path: Path) -> None:
    cfg, raw = populated
    results = tmp_path / "results"
    write_all(cfg, results, raw)
    readme = tmp_path / "README.md"
    readme.write_text("<!-- BC:BEGIN before_after -->\nno end marker\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatched"):
        readme_inject(readme, results)
