"""The error foundry: every corruption is deterministic, documented, and
annotated with the catch the gate is expected to produce.

Tier 1: swap_amount transposes adjacent digits of the FIRST dollar amount
(the result parses to a different positive cents value); fabricate_id swaps
one real invoice ID for an INV-9xxx absent from the ledger; break_sum
perturbs exactly the rendered net-balance string. Each returns None when its
precondition is absent. Tier 2 is a fixed list of naive directives; Tier 3
is a note, not a corruption; Tier 4 appends exactly one unsupported sentence.

Hermetic: the ledger is constructed inline; no file, network, or model.
"""

from __future__ import annotations

import re
from datetime import date

from balancecheck.bench.corrupt import (
    FUZZY_SENTENCES,
    CorruptedDraft,
    break_sum,
    fabricate_id,
    inject_fuzzy,
    swap_amount,
    tier2_instructions,
    tier3_note,
)
from balancecheck.contracts.models import (
    ClientInfo,
    CreditMemo,
    Invoice,
    Ledger,
    Payment,
)
from balancecheck.substrate import derive
from balancecheck.substrate.money import parse_dollars, render

_DOLLAR_RE = re.compile(r"-?\$[\d,]+(?:\.\d{1,2})?")


def make_ledger() -> Ledger:
    """Net balance: 240000 + 76000 - 50000 - 15000 = 251000 ($2,510.00)."""
    return Ledger(
        scenario_id="S-T-05",
        pool="A",
        client=ClientInfo(id="CL-T", name="Test Client", terms_days=30, as_of=date(2026, 3, 31)),
        invoices=[
            Invoice(id="INV-1001", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=240000),
            Invoice(id="INV-1002", date=date(2026, 3, 1), due=date(2026, 3, 31), amount_cents=76000),
        ],
        payments=[Payment(id="PMT-2001", date=date(2026, 3, 10), amount_cents=50000)],
        credit_memos=[CreditMemo(id="CM-3001", date=date(2026, 3, 12), amount_cents=15000)],
        applications=[],
    )


def good_draft(ledger: Ledger) -> str:
    net = render(derive.net_balance(ledger))
    assert net == "$2,510.00"
    return (
        f"As of 2026-03-31 your net balance due is {net}. Invoice INV-1001 is"
        " open for $2,400.00 and invoice INV-1002 for $760.00. We hold an"
        " unapplied payment PMT-2001 of $500.00 and an open credit memo"
        " CM-3001 of $150.00."
    )


# ---------------------------------------------------------------------------
# Tier 1: swap_amount
# ---------------------------------------------------------------------------


def test_swap_amount_transposes_the_first_amount() -> None:
    ledger = make_ledger()
    draft = good_draft(ledger)
    corrupted = swap_amount(draft, ledger)
    assert isinstance(corrupted, CorruptedDraft)
    assert corrupted.tier == 1
    assert corrupted.scenario_id == "S-T-05"
    assert corrupted.expected_kind == "c_amt_or_sum_fail"
    assert corrupted.expected_action == "revise"
    assert corrupted.draft != draft

    original_first = _DOLLAR_RE.search(draft)
    corrupted_first = _DOLLAR_RE.search(corrupted.draft)
    assert original_first is not None and corrupted_first is not None
    assert original_first.group(0) == "$2,510.00"
    # The transposed token parses to a different positive cents value.
    old_value = parse_dollars(original_first.group(0))
    new_value = parse_dollars(corrupted_first.group(0))
    assert new_value != old_value
    assert new_value > 0
    assert sorted(str(int(new_value))) == sorted(str(int(old_value)))  # a pure transposition
    # Only the first amount changed; everything around it is intact.
    assert corrupted.draft.startswith(draft[: original_first.start()])
    assert corrupted.draft.endswith(draft[original_first.end() :])


def test_swap_amount_matches_the_documented_example() -> None:
    ledger = make_ledger()
    corrupted = swap_amount("The invoice total is $2,400.00 as agreed.", ledger)
    assert corrupted is not None
    # 240000 transposed in the documented style: 204000.
    assert "$2,040.00" in corrupted.draft
    assert "$2,400.00" not in corrupted.draft


def test_swap_amount_stays_positive_on_small_values() -> None:
    ledger = make_ledger()
    corrupted = swap_amount("A residual of $0.10 remains.", ledger)
    assert corrupted is not None
    match = _DOLLAR_RE.search(corrupted.draft)
    assert match is not None
    assert parse_dollars(match.group(0)) > 0
    assert parse_dollars(match.group(0)) != 10


def test_swap_amount_skips_when_no_amount_present() -> None:
    ledger = make_ledger()
    assert swap_amount("No figures appear in this reply.", ledger) is None


def test_swap_amount_skips_when_no_differing_adjacent_digits() -> None:
    ledger = make_ledger()
    # $0.11 is 11 cents: the only adjacent pair is identical, untransposable.
    assert swap_amount("A rounding residual of $0.11 remains.", ledger) is None


# ---------------------------------------------------------------------------
# Tier 1: fabricate_id
# ---------------------------------------------------------------------------


def test_fabricate_id_introduces_an_absent_invoice_id() -> None:
    ledger = make_ledger()
    draft = good_draft(ledger)
    corrupted = fabricate_id(draft, ledger)
    assert isinstance(corrupted, CorruptedDraft)
    assert corrupted.tier == 1
    assert corrupted.expected_kind == "c_exist_fail"
    assert corrupted.expected_action == "escalate"

    fakes = re.findall(r"INV-9\d{3}\b", corrupted.draft)
    assert len(fakes) == 1
    assert fakes[0] not in derive.document_ids(ledger)
    # Exactly one occurrence of the real ID was replaced (INV-1001 comes
    # first in the draft), and the other invoice ID is untouched.
    assert corrupted.draft.count("INV-1001") == draft.count("INV-1001") - 1
    assert corrupted.draft.count("INV-1002") == draft.count("INV-1002")


def test_fabricate_id_skips_when_no_invoice_id_present() -> None:
    ledger = make_ledger()
    assert fabricate_id("Your balance is $2,510.00, reference PMT-2001.", ledger) is None


# ---------------------------------------------------------------------------
# Tier 1: break_sum
# ---------------------------------------------------------------------------


def test_break_sum_changes_exactly_the_net_balance_string() -> None:
    ledger = make_ledger()
    draft = good_draft(ledger)
    corrupted = break_sum(draft, ledger)
    assert isinstance(corrupted, CorruptedDraft)
    assert corrupted.tier == 1
    assert corrupted.expected_kind == "c_sum_fail"
    assert corrupted.expected_action == "revise"
    # 251000 + 1000 = 252000: the perturbation is the documented delta, and
    # nothing but the net-balance token moved.
    assert corrupted.draft == draft.replace("$2,510.00", "$2,520.00", 1)
    assert "$2,510.00" not in corrupted.draft
    assert "$2,400.00" in corrupted.draft  # itemized amounts untouched


def test_break_sum_skips_when_net_balance_absent() -> None:
    ledger = make_ledger()
    draft = "Please review invoices INV-1001 and INV-1002 for $2,400.00 and $760.00."
    assert break_sum(draft, ledger) is None


# ---------------------------------------------------------------------------
# Tier 2 and Tier 3
# ---------------------------------------------------------------------------


def test_tier2_instructions_are_the_three_naive_directives() -> None:
    pairs = tier2_instructions()
    assert len(pairs) == 3
    ids = [error_id for error_id, _ in pairs]
    assert len(set(ids)) == 3
    instructions = [instruction for _, instruction in pairs]
    assert any("sum of all unpaid invoice amounts" in i for i in instructions)
    assert any("full original amount" in i for i in instructions)
    assert any("credit memos as already applied" in i for i in instructions)
    for _, instruction in pairs:
        assert instruction.strip()


def test_tier3_note_states_the_abstain_only_catch() -> None:
    note = tier3_note()
    assert "not a corruption" in note
    assert "06" in note
    assert "abstain" in note
    assert "escalate" in note


# ---------------------------------------------------------------------------
# Tier 4: inject_fuzzy
# ---------------------------------------------------------------------------


def test_inject_fuzzy_appends_exactly_one_sentence() -> None:
    ledger = make_ledger()
    draft = good_draft(ledger)
    corrupted = inject_fuzzy(draft, scenario_id=ledger.scenario_id)
    assert isinstance(corrupted, CorruptedDraft)
    assert corrupted.tier == 4
    assert corrupted.scenario_id == "S-T-05"
    assert corrupted.expected_kind == "fuzzy_unsupported"
    assert corrupted.expected_action == "revise_or_escalate"
    assert corrupted.draft.startswith(draft.rstrip())
    appended = [s for s in FUZZY_SENTENCES if s in corrupted.draft]
    assert len(appended) == 1
    assert corrupted.draft == draft.rstrip() + "\n\n" + appended[0]
    assert corrupted.draft.count(appended[0]) == 1


def test_inject_fuzzy_is_deterministic() -> None:
    ledger = make_ledger()
    draft = good_draft(ledger)
    first = inject_fuzzy(draft, scenario_id=ledger.scenario_id)
    second = inject_fuzzy(draft, scenario_id=ledger.scenario_id)
    assert first == second
