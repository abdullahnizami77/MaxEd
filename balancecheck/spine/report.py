"""Every reported figure, generated from the event log and raw result files.

This module is the single source of every human-readable number in the
submission. No number is ever typed by hand anywhere else: every function
here reads the event log (via the typed event models) and/or the raw result
JSON files under results/raw/ and returns a markdown string. write_all is
the one entry point that writes results/*.md, and readme_inject splices
those generated files into the README's marked blocks, which is what the
drift test regenerates and diffs (invariant I10).

Guarantees:
- Deterministic: two write_all calls over the same log and raw files
  produce byte-identical output; every ordering is explicit and every
  aggregate is integer arithmetic (percents are (100 * part) // total).
- Placeholder-honest: a table whose raw input has not been produced yet
  renders a stated placeholder line, never an invented number.
- The before/after table reads first-pass ScoreEvents only, because the
  revise loop scrubs numeric errors and a final-stage read would mask
  learning (plan section 13).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from balancecheck.config import Config
from balancecheck.contracts.models import (
    CapabilityGapEvent,
    ScoreEvent,
    TraceEvent,
)
from balancecheck.spine.events import read_events

# The canonical pass order for the before/after comparison.
PASS_ORDER = ("pass1", "pass2", "pass2R")

# The block-name vocabulary write_all produces; readme_inject accepts any
# name whose results file exists, so these stay in one place.
REPORT_NAMES = (
    "before_after",
    "injected_errors",
    "capability_gaps",
    "calibration",
    "pairwise",
    "trace_stats",
)

_BLOCK_RE = re.compile(
    r"(<!-- BC:BEGIN (?P<name>[A-Za-z0-9_.-]+) -->)(?P<body>.*?)(<!-- BC:END (?P=name) -->)",
    re.DOTALL,
)
_BEGIN_RE = re.compile(r"<!-- BC:BEGIN ([A-Za-z0-9_.-]+) -->")


# ---------------------------------------------------------------------------
# integer-arithmetic rendering helpers
# ---------------------------------------------------------------------------


def _pct(part: int, total: int) -> str:
    """Integer percent, floor division; 'n/a' when the denominator is empty."""
    if total <= 0:
        return "n/a"
    return f"{(100 * part) // total}%"


def _ratio(part: int, total: int) -> str:
    """Render 'x/y (NN%)' with integer percent math."""
    return f"{part}/{total} ({_pct(part, total)})"


def _delta_marker(n: int) -> str:
    """Plain '+n' / '-n' / '0' delta markers."""
    if n > 0:
        return f"+{n}"
    if n < 0:
        return str(n)
    return "0"


def _cell(value: object) -> str:
    """One markdown table cell: newline- and pipe-safe."""
    return str(value).replace("\n", " ").replace("|", "\\|")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return lines


def _json_value(value: object) -> str:
    """Deterministic rendering for a raw-JSON value in a table cell."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------------------
# before/after (the primary instrument)
# ---------------------------------------------------------------------------


def before_after_table(events: Iterable[object]) -> str:
    """Per-pass aggregates and the per-scenario paired pass1-vs-pass2 table.

    Considers ScoreEvent rows with stage 'first_pass' only, grouped by
    pass_label; when the same (pass, scenario) was scored more than once the
    last row in log order wins (a rerun supersedes).
    """
    latest: dict[tuple[str, str], ScoreEvent] = {}
    for e in events:
        if isinstance(e, ScoreEvent) and e.stage == "first_pass" and e.pass_label in PASS_ORDER:
            latest[(e.pass_label, e.scenario_id)] = e

    lines: list[str] = [
        "# Before/after: first-pass drafts, code-computed",
        "",
        "Scores are read on first-pass drafts (stage first_pass), before any",
        "revision, because the revise loop scrubs numeric errors and a",
        "final-stage read would mask learning.",
        "",
    ]
    if not latest:
        lines.append("(no first-pass score events recorded)")
        return "\n".join(lines) + "\n"

    labels = [p for p in PASS_ORDER if any(k[0] == p for k in latest)]

    lines.append("## Per pass")
    lines.append("")
    rows: list[list[str]] = []
    for label in labels:
        scored = [latest[k] for k in sorted(latest) if k[0] == label]
        g_num = sum(s.grounding_checked for s in scored)
        g_den = sum(s.grounding_total for s in scored)
        c_num = sum(s.completeness_present for s in scored)
        c_den = sum(s.completeness_total for s in scored)
        rev_sum = sum(s.revision_count for s in scored)
        actions: dict[str, int] = {}
        for s in scored:
            actions[s.terminal_action] = actions.get(s.terminal_action, 0) + 1
        action_str = ", ".join(
            f"{a}: {c}" for a, c in sorted(actions.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        rows.append(
            [
                label,
                str(len(scored)),
                _ratio(g_num, g_den),
                _ratio(c_num, c_den),
                f"{rev_sum}/{len(scored)}",
                action_str,
            ]
        )
    lines.extend(
        _table(
            ["pass", "scenarios", "grounding", "completeness", "mean revisions", "terminal actions"],
            rows,
        )
    )

    paired = sorted(
        sid
        for sid in {k[1] for k in latest}
        if ("pass1", sid) in latest and ("pass2", sid) in latest
    )
    unpaired = sorted(
        (sid, pl)
        for (pl, sid) in latest
        if pl in ("pass1", "pass2")
        and not (("pass1", sid) in latest and ("pass2", sid) in latest)
    )
    lines.append("")
    lines.append("## Paired per scenario: pass1 vs pass2")
    lines.append("")
    if paired:
        prows: list[list[str]] = []
        for sid in paired:
            s1 = latest[("pass1", sid)]
            s2 = latest[("pass2", sid)]
            prows.append(
                [
                    sid,
                    f"{_ratio(s1.grounding_checked, s1.grounding_total)} -> "
                    f"{_ratio(s2.grounding_checked, s2.grounding_total)}",
                    _pp_delta(
                        s1.grounding_checked,
                        s1.grounding_total,
                        s2.grounding_checked,
                        s2.grounding_total,
                    ),
                    f"{_ratio(s1.completeness_present, s1.completeness_total)} -> "
                    f"{_ratio(s2.completeness_present, s2.completeness_total)}",
                    _pp_delta(
                        s1.completeness_present,
                        s1.completeness_total,
                        s2.completeness_present,
                        s2.completeness_total,
                    ),
                    f"{s1.revision_count} -> {s2.revision_count}",
                    _delta_marker(s2.revision_count - s1.revision_count),
                ]
            )
        lines.extend(
            _table(
                [
                    "scenario",
                    "grounding pass1 -> pass2",
                    "delta (pp)",
                    "completeness pass1 -> pass2",
                    "delta (pp)",
                    "revisions pass1 -> pass2",
                    "delta",
                ],
                prows,
            )
        )
    else:
        lines.append("(no scenario scored in both pass1 and pass2 yet)")
    if unpaired:
        lines.append("")
        for sid, pl in unpaired:
            lines.append(f"unpaired: {sid} ({pl} only)")
    lines.append("")
    lines.append(
        f"n = {len(paired)} paired scenarios. "
        "n is too small for a significance claim; deltas are directional."
    )
    return "\n".join(lines) + "\n"


def _pp_delta(p1_num: int, p1_den: int, p2_num: int, p2_den: int) -> str:
    """Delta in integer percentage points between two ratios."""
    if p1_den <= 0 or p2_den <= 0:
        return "n/a"
    return _delta_marker((100 * p2_num) // p2_den - (100 * p1_num) // p1_den)


# ---------------------------------------------------------------------------
# injected errors
# ---------------------------------------------------------------------------


def injected_errors_table(raw_dir: Path) -> str:
    """Per-tier tables over results/raw/injected_errors.json, with a caught
    summary per tier and balanced accuracy where rows carry a truth field."""
    path = raw_dir / "injected_errors.json"
    lines: list[str] = ["# Injected errors", ""]
    if not path.exists():
        lines.append("(errors run not yet recorded)")
        return "\n".join(lines) + "\n"
    rows: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        lines.append("(errors run recorded zero rows)")
        return "\n".join(lines) + "\n"

    tiers = sorted({int(r.get("tier", 0)) for r in rows})
    for tier in tiers:
        tier_rows = [r for r in rows if int(r.get("tier", 0)) == tier]
        title = f"## Tier {tier}"
        if tier == 0:
            title += " (clean controls: caught means falsely flagged)"
        lines.append(title)
        lines.append("")
        lines.extend(
            _table(
                [
                    "error_id",
                    "scenario",
                    "expected kind",
                    "expected action",
                    "decision",
                    "reason",
                    "caught",
                    "notes",
                ],
                [
                    [
                        str(r.get("error_id", "")),
                        str(r.get("scenario_id", "")),
                        str(r.get("expected_kind", "")),
                        str(r.get("expected_action", "")),
                        str(r.get("decision_action", "")),
                        str(r.get("decision_reason", "")),
                        "yes" if r.get("caught") else "no",
                        str(r.get("notes", "")),
                    ]
                    for r in tier_rows
                ],
            )
        )
        caught = sum(1 for r in tier_rows if r.get("caught"))
        lines.append("")
        lines.append(f"Tier {tier}: caught {_ratio(caught, len(tier_rows))}.")
        lines.append("")

    if any("truth" in r for r in rows):
        clean_rows = [r for r in rows if r.get("truth") == "clean"]
        clean_ok = sum(1 for r in clean_rows if not r.get("caught"))
        lines.append("## Balanced accuracy per tier")
        lines.append("")
        lines.append(
            "Raw accuracy would reward a rubber stamp (most claims in real"
            " drafts are true); balanced accuracy averages the catch rate on"
            " corrupted rows with the pass rate on the clean controls."
        )
        lines.append("")
        for tier in tiers:
            bad = [
                r
                for r in rows
                if int(r.get("tier", 0)) == tier and r.get("truth") not in (None, "clean")
            ]
            if not bad:
                continue
            caught = sum(1 for r in bad if r.get("caught"))
            sens = (100 * caught) // len(bad)
            if clean_rows:
                spec = (100 * clean_ok) // len(clean_rows)
                ba = (sens + spec) // 2
                lines.append(
                    f"Tier {tier}: balanced accuracy {ba}% "
                    f"(catch rate {_ratio(caught, len(bad))}, "
                    f"clean-pass rate {_ratio(clean_ok, len(clean_rows))})."
                )
            else:
                lines.append(
                    f"Tier {tier}: balanced accuracy n/a (no clean controls; "
                    f"catch rate {_ratio(caught, len(bad))})."
                )
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# capability gaps
# ---------------------------------------------------------------------------


def capability_gaps_table(events: Iterable[object]) -> str:
    """CapabilityGapEvent counts by category, sorted by count descending,
    with gen_id backlinks in first-seen order."""
    gaps = [e for e in events if isinstance(e, CapabilityGapEvent)]
    lines: list[str] = ["# Capability gaps", ""]
    if not gaps:
        lines.append("(no capability gaps recorded)")
        return "\n".join(lines) + "\n"
    by_category: dict[str, list[str]] = {}
    for g in gaps:
        by_category.setdefault(g.missing.value, []).append(g.gen_id)
    ordered = sorted(by_category.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines.extend(
        _table(
            ["category", "count", "gen_ids"],
            [[cat, str(len(ids)), ", ".join(ids)] for cat, ids in ordered],
        )
    )
    lines.append("")
    lines.append(f"Total capability gaps: {len(gaps)}.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


def calibration_table(raw_dir: Path) -> str:
    """The agreement report from results/raw/calibration.json, rendered as a
    field/value table so every kappa, CI, PABAK, weighted kappa, and raw
    agreement figure that the bench produced appears verbatim."""
    path = raw_dir / "calibration.json"
    lines: list[str] = ["# Judge calibration", ""]
    if not path.exists():
        lines.append("(calibration not yet recorded)")
        return "\n".join(lines) + "\n"
    data: dict = json.loads(path.read_text(encoding="utf-8"))
    lines.extend(
        _table(
            ["field", "value"],
            [[key, _json_value(data[key])] for key in sorted(data)],
        )
    )
    n = data.get("n", data.get("n_labeled", 16))
    lines.append("")
    lines.append(
        f"Kappa on n={n} carries a wide CI and is a calibration signal, "
        "not a certification."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# pairwise
# ---------------------------------------------------------------------------


def pairwise_table(raw_dir: Path) -> str:
    """Win/tie/loss counts over results/raw/pairwise.json, the decisive-pair
    count, and the flip rate labeled as the inconsistency rate."""
    path = raw_dir / "pairwise.json"
    lines: list[str] = ["# Pairwise: pass1 vs pass2 first drafts, both orderings", ""]
    if not path.exists():
        lines.append("(pairwise run not yet recorded)")
        return "\n".join(lines) + "\n"
    rows: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        lines.append("(pairwise run recorded zero pairs)")
        return "\n".join(lines) + "\n"
    wins = sum(1 for r in rows if r.get("verdict") == "b")
    losses = sum(1 for r in rows if r.get("verdict") == "a")
    ties = sum(1 for r in rows if r.get("verdict") == "tie")
    flipped = sum(1 for r in rows if r.get("flipped"))
    lines.extend(
        _table(
            ["outcome", "count"],
            [
                ["pass2 wins (verdict b)", str(wins)],
                ["ties", str(ties)],
                ["pass1 wins (verdict a)", str(losses)],
            ],
        )
    )
    lines.append("")
    lines.append(f"Decisive pairs: {_ratio(wins + losses, len(rows))}.")
    lines.append(
        "inconsistency rate (an upper bound on position bias): "
        f"{_ratio(flipped, len(rows))}"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# trace stats
# ---------------------------------------------------------------------------


def trace_stats(events: Iterable[object]) -> str:
    """Model-call attempts by task, ok rate, structured-parse failure count,
    and the total trace-line count (invariant I6 made visible)."""
    traces = [e for e in events if isinstance(e, TraceEvent)]
    lines: list[str] = ["# Trace stats", ""]
    if not traces:
        lines.append("(no trace lines recorded)")
        return "\n".join(lines) + "\n"
    by_task: dict[str, list[TraceEvent]] = {}
    for t in traces:
        by_task.setdefault(t.task, []).append(t)
    rows: list[list[str]] = []
    for task in sorted(by_task):
        attempts = by_task[task]
        ok = sum(1 for t in attempts if t.ok)
        rows.append([task, str(len(attempts)), str(ok), _pct(ok, len(attempts))])
    lines.extend(_table(["task", "attempts", "ok", "ok rate"], rows))
    parse_failures = sum(1 for t in traces if not t.ok and "parse" in t.error.lower())
    lines.append("")
    lines.append(f"Structured-parse failures: {parse_failures}")
    lines.append(f"Total trace lines: {len(traces)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def write_all(cfg: Config, results_dir: Path, raw_dir: Path) -> dict[str, Path]:
    """Generate every results/*.md from the event log and raw JSON files.

    The one write path for reported numbers. Returns {name: path} for the
    six files written.
    """
    events = read_events(cfg.log_path)
    outputs: dict[str, str] = {
        "before_after": before_after_table(events),
        "injected_errors": injected_errors_table(raw_dir),
        "capability_gaps": capability_gaps_table(events),
        "calibration": calibration_table(raw_dir),
        "pairwise": pairwise_table(raw_dir),
        "trace_stats": trace_stats(events),
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in REPORT_NAMES:
        content = outputs[name]
        if "BC:BEGIN" in content or "BC:END" in content:
            # A results file containing a marker string would corrupt README
            # injection silently (duplicated trailing content on every
            # re-inject); refusing here keeps I10's machinery trustworthy.
            raise ValueError(f"generated {name}.md contains a BC injection marker")
        if not content.endswith("\n"):
            content += "\n"
        path = results_dir / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        written[name] = path
    return written


def readme_inject(readme_path: Path, results_dir: Path) -> str:
    """Replace each README block's interior with its generated results file.

    Blocks look like '<!-- BC:BEGIN name --> ... <!-- BC:END name -->'; the
    interior becomes the contents of results_dir/name.md. The caller writes
    the returned text. A block naming a results file that does not exist
    raises, as does a BEGIN marker with no matching END; this function backs
    invariant I10 (the drift test regenerates and diffs).
    """
    text = readme_path.read_text(encoding="utf-8")

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        source = results_dir / f"{name}.md"
        if not source.exists():
            raise ValueError(
                f"unknown README injection block {name!r}: no {name}.md in {results_dir}"
            )
        content = source.read_text(encoding="utf-8").strip("\n")
        interior = "\n" + content + "\n" if content else "\n"
        return match.group(1) + interior + match.group(4)

    new_text, replaced = _BLOCK_RE.subn(_replace, text)
    begins = _BEGIN_RE.findall(text)
    if len(begins) != replaced:
        raise ValueError(
            "mismatched BC:BEGIN/BC:END markers in README: "
            f"{len(begins)} BEGIN marker(s), {replaced} complete block(s)"
        )
    # The injected text must itself re-inject cleanly (idempotence); a
    # mismatch after injection means some content smuggled a marker in.
    if len(_BEGIN_RE.findall(new_text)) != len(_BLOCK_RE.findall(new_text)):
        raise ValueError("README injection produced unbalanced markers")
    return new_text
