"""The error foundry: reproducible corruptions with expected-catch annotations.

What this module guarantees (plan v3, section 13, "Error foundry"):

- Tier 1 functions (swap_amount, fabricate_id, break_sum) are pure string
  corruptions over a known-good draft. Each returns a CorruptedDraft carrying
  the expected_kind and expected_action the gate must produce, or None when
  the draft lacks the precondition (no dollar amount, no invoice ID, no
  rendered net balance), and the caller records the skip. These live in the
  code layer's competence zone by construction, so a 100 percent catch rate
  proves the code checks are wired, nothing more, and the report says so.
- Tier 2 (tier2_instructions) does not corrupt text; it returns naive
  drafting directives the integrator runs live against the untouched ledger,
  expected to surface as deterministic C-SUM/C-AMT failures caught by code.
- Tier 3 is not a corruption at all; tier3_note() returns the sentence the
  report carries for it.
- Tier 4 (inject_fuzzy) appends exactly one unsupported sentence from a
  fixed list, aimed at the model verifier's false-negative rate.

Every corruption is deterministic: same draft and ledger in, same corrupted
draft out. All money is integer cents through balancecheck.substrate.money;
no float literal appears in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from balancecheck.contracts.models import Ledger
from balancecheck.substrate import derive
from balancecheck.substrate.money import cents, parse_dollars, render

_DOLLAR_RE = re.compile(r"-?\$[\d,]+(?:\.\d{1,2})?")

# Tier 4: the fixed list of unsupported sentences (no ledger supports these).
FUZZY_SENTENCES: tuple[str, ...] = (
    "Your account manager approved a waiver of the late fees on this balance.",
    "As we discussed by phone, we will hold this invoice until next quarter.",
)


@dataclass
class CorruptedDraft:
    """One corrupted draft plus the annotation the catch-rate report reads."""

    error_id: str
    tier: int
    scenario_id: str
    draft: str
    expected_kind: str
    expected_action: str
    description: str


# ---------------------------------------------------------------------------
# Tier 1: programmatic string corruptions
# ---------------------------------------------------------------------------


def _transpose_adjacent_digits(value: int) -> int | None:
    """Transpose two adjacent differing digits of a positive cents value.

    The classic keying error: 240000 becomes 204000. The swap position is
    deterministic: the first adjacent differing pair at index >= 1 when one
    exists (so the leading digit is preserved and magnitude stays plausible),
    else the pair at index 0. Returns None when the value has no adjacent
    differing digits to swap, or when the result would not be a different
    positive value.
    """
    if value <= 0:
        return None
    digits = list(str(value))
    candidates = [i for i in range(len(digits) - 1) if digits[i] != digits[i + 1]]
    if not candidates:
        return None
    pick = next((i for i in candidates if i >= 1), candidates[0])
    digits[pick], digits[pick + 1] = digits[pick + 1], digits[pick]
    corrupted = int("".join(digits))
    if corrupted <= 0 or corrupted == value:
        return None
    return corrupted


def swap_amount(draft: str, ledger: Ledger) -> CorruptedDraft | None:
    """Transpose two adjacent digits of the FIRST dollar amount in the draft.

    Expected catch: a C-AMT or C-SUM failure (the transposed value matches
    nothing the ledger supports), routed to revise. Returns None when the
    draft carries no dollar amount, or the first amount has no transposable
    digit pair.
    """
    match = _DOLLAR_RE.search(draft)
    if match is None:
        return None
    token = match.group(0)
    value = int(parse_dollars(token))
    corrupted_value = _transpose_adjacent_digits(value)
    if corrupted_value is None:
        return None
    corrupted_token = render(cents(corrupted_value))
    corrupted = draft[: match.start()] + corrupted_token + draft[match.end() :]
    return CorruptedDraft(
        error_id=f"T1-SWAP-{ledger.scenario_id}",
        tier=1,
        scenario_id=ledger.scenario_id,
        draft=corrupted,
        expected_kind="c_amt_or_sum_fail",
        expected_action="revise",
        description=(
            f"transposed adjacent digits of the first amount {token},"
            f" yielding {corrupted_token}"
        ),
    )


def fabricate_id(draft: str, ledger: Ledger) -> CorruptedDraft | None:
    """Replace one occurrence of a real invoice ID with a fabricated INV-9xxx.

    The fabricated ID is INV-9 plus three digits, chosen deterministically as
    the first candidate present neither in the ledger nor already in the
    draft. Expected catch: C-EXIST failure, escalated immediately. Returns
    None when no ledger invoice ID appears in the draft.
    """
    in_draft = [(draft.find(i.id), i.id) for i in ledger.invoices if i.id in draft]
    if not in_draft:
        return None
    _, real_id = min(in_draft)
    known_ids = derive.document_ids(ledger)
    fake_id = ""
    for n in range(1000):
        candidate = f"INV-9{n:03d}"
        if candidate not in known_ids and candidate not in draft:
            fake_id = candidate
            break
    if not fake_id:
        return None
    corrupted = draft.replace(real_id, fake_id, 1)
    return CorruptedDraft(
        error_id=f"T1-FABID-{ledger.scenario_id}",
        tier=1,
        scenario_id=ledger.scenario_id,
        draft=corrupted,
        expected_kind="c_exist_fail",
        expected_action="escalate",
        description=f"replaced one occurrence of {real_id} with fabricated {fake_id}",
    )


def break_sum(draft: str, ledger: Ledger) -> CorruptedDraft | None:
    """Perturb the rendered net-balance string by a plausible delta.

    Finds money.render(derive.net_balance(ledger)) in the draft, adds 1000
    cents, and re-renders. Expected catch: C-SUM failure, routed to revise.
    Returns None when the rendered net balance does not appear in the draft.
    """
    net = derive.net_balance(ledger)
    net_token = render(net)
    if net_token not in draft:
        return None
    broken_token = render(cents(int(net) + 1000))
    corrupted = draft.replace(net_token, broken_token, 1)
    return CorruptedDraft(
        error_id=f"T1-SUM-{ledger.scenario_id}",
        tier=1,
        scenario_id=ledger.scenario_id,
        draft=corrupted,
        expected_kind="c_sum_fail",
        expected_action="revise",
        description=(
            f"perturbed the stated net balance {net_token} to {broken_token}"
            " (added 1000 cents)"
        ),
    )


# ---------------------------------------------------------------------------
# Tier 2: prompted misreadings (run live by the integrator)
# ---------------------------------------------------------------------------


def tier2_instructions() -> list[tuple[str, str]]:
    """Naive drafting directives producing deterministic code-caught errors.

    Each pair is (error_id, instruction). The integrator runs the drafter
    with the instruction against the untouched ledger; the misreading
    surfaces as C-SUM or C-AMT failures caught by code.
    """
    return [
        (
            "T2-SUM-IGNORES-UNAPPLIED",
            "State the balance due as the sum of all unpaid invoice amounts."
            " Ignore any payments that are not applied to an invoice.",
        ),
        (
            "T2-FULL-ORIGINAL-AMOUNTS",
            "List every invoice on the account as owing its full original amount."
            " Do not check payments.",
        ),
        (
            "T2-CREDITS-ASSUMED-APPLIED",
            "Treat any credit memos as already applied and do not mention them.",
        ),
    ]


# ---------------------------------------------------------------------------
# Tier 3: not a corruption
# ---------------------------------------------------------------------------


def tier3_note() -> str:
    """The report's sentence for Tier 3, which injects nothing."""
    return (
        "Tier 3 is not a corruption: scenario 06 (ambiguous allocation) is graded"
        " on the gate's own behavior, where only abstain or escalate counts as a"
        " catch and any confident draft is a miss."
    )


# ---------------------------------------------------------------------------
# Tier 4: fuzzy unsupported claims
# ---------------------------------------------------------------------------


def inject_fuzzy(draft: str, scenario_id: str = "") -> CorruptedDraft:
    """Append exactly one unsupported sentence from the fixed list.

    The sentence asserts support the records do not give, so the code layer
    cannot catch it; this tier measures the model verifier's false-negative
    rate. Selection is deterministic (draft length modulo list size).
    """
    index = len(draft) % len(FUZZY_SENTENCES)
    sentence = FUZZY_SENTENCES[index]
    corrupted = draft.rstrip() + "\n\n" + sentence
    return CorruptedDraft(
        error_id=f"T4-FUZZY-{index}",
        tier=4,
        scenario_id=scenario_id,
        draft=corrupted,
        expected_kind="fuzzy_unsupported",
        expected_action="revise_or_escalate",
        description=f"appended one unsupported sentence: {sentence!r}",
    )
