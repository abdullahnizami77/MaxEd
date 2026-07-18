"""The code-grounded scorer's behavior, branch by branch.

grounding: only code-checkable claim types count; PASS alone reaches the
numerator; FAIL, UNVERIFIABLE, and never-checked claims sit in the
denominator (an unresolvable number is an unverified number). completeness:
applicability-gated, so N/A items are excluded from the denominator and
simpler ledgers are not penalized; every item's present and absent branch is
exercised. build_score_event fills the ScoreEvent exactly.

Hermetic: every ledger is constructed inline; no file, network, or model.
"""

from __future__ import annotations

from datetime import date

from balancecheck.bench.score import build_score_event, completeness, grounding
from balancecheck.contracts.models import (
    Application,
    CheckResult,
    CheckStatus,
    Claim,
    ClaimType,
    ClientInfo,
    CreditMemo,
    Invoice,
    Ledger,
    Payment,
)
from balancecheck.substrate import derive
from balancecheck.substrate.money import render

AS_OF = date(2026, 3, 31)


def _client() -> ClientInfo:
    return ClientInfo(id="CL-T", name="Test Client", terms_days=30, as_of=AS_OF)


def rich_ledger() -> Ledger:
    """Two open invoices, one unapplied payment, one open credit memo."""
    return Ledger(
        scenario_id="S-T-01",
        pool="A",
        client=_client(),
        invoices=[
            Invoice(id="INV-1001", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=240000),
            Invoice(id="INV-1002", date=date(2026, 3, 1), due=date(2026, 3, 31), amount_cents=76000),
        ],
        payments=[Payment(id="PMT-2001", date=date(2026, 3, 10), amount_cents=50000)],
        credit_memos=[CreditMemo(id="CM-3001", date=date(2026, 3, 12), amount_cents=15000)],
        applications=[],
    )


def settled_ledger() -> Ledger:
    """One invoice fully paid by one fully applied payment: only the net
    balance item is applicable."""
    return Ledger(
        scenario_id="S-T-02",
        pool="A",
        client=_client(),
        invoices=[
            Invoice(id="INV-1101", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=100000),
        ],
        payments=[Payment(id="PMT-2101", date=date(2026, 3, 1), amount_cents=100000)],
        credit_memos=[],
        applications=[
            Application(source_id="PMT-2101", target_invoice="INV-1101", amount_cents=100000),
        ],
    )


def _claim(
    claim_id: str,
    claim_type: ClaimType,
    token: str,
    status: CheckStatus | None,
) -> Claim:
    result = None if status is None else CheckResult(status=status)
    return Claim(
        claim_id=claim_id,
        type=claim_type,
        span=f"a sentence containing {token}",
        token=token,
        check_result=result,
    )


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------


def test_grounding_counts_only_passes_in_numerator() -> None:
    claims = [
        _claim("c1", ClaimType.C_AMT, "$2,400.00", CheckStatus.PASS),
        _claim("c2", ClaimType.C_EXIST, "INV-1001", CheckStatus.PASS),
        _claim("c3", ClaimType.C_SUM, "$9,999.00", CheckStatus.FAIL),
        _claim("c4", ClaimType.C_AMT, "a quarter of the invoice", CheckStatus.UNVERIFIABLE),
        _claim("c5", ClaimType.C_DATE, "2026-03-03", None),  # never checked
    ]
    assert grounding(claims) == (2, 5)


def test_grounding_excludes_fuzzy_claims_entirely() -> None:
    claims = [
        _claim("c1", ClaimType.C_FUZZY, "we appreciate your business", CheckStatus.PASS),
        _claim("c2", ClaimType.C_FUZZY, "your manager approved this", CheckStatus.UNSUPPORTED),
        _claim("c3", ClaimType.C_SUM, "$2,510.00", CheckStatus.PASS),
    ]
    assert grounding(claims) == (1, 1)


def test_grounding_empty_claims() -> None:
    assert grounding([]) == (0, 0)


def test_grounding_unverifiable_and_fail_hold_denominator() -> None:
    claims = [
        _claim("c1", ClaimType.C_AMT, "$1.00", CheckStatus.UNVERIFIABLE),
        _claim("c2", ClaimType.C_SUM, "$2.00", CheckStatus.FAIL),
    ]
    assert grounding(claims) == (0, 2)


# ---------------------------------------------------------------------------
# completeness
# ---------------------------------------------------------------------------


def test_completeness_all_items_present() -> None:
    ledger = rich_ledger()
    net = render(derive.net_balance(ledger))  # $2,510.00
    draft = (
        f"As of 2026-03-31 your net balance due is {net}. Open invoices:"
        " INV-1001 for $2,400.00 and INV-1002 for $760.00. We also hold an"
        " unapplied payment PMT-2001 and an open credit memo CM-3001."
    )
    assert completeness(draft, ledger) == (4, 4)


def test_completeness_rendered_amounts_stand_in_for_ids() -> None:
    ledger = rich_ledger()
    net = render(derive.net_balance(ledger))
    # The unapplied payment appears only as $500.00 and the open credit only
    # as $150.00; both count as mentioned.
    draft = (
        f"Your balance is {net}, covering INV-1001 and INV-1002. A payment of"
        " $500.00 remains unapplied and a credit of $150.00 is open."
    )
    assert completeness(draft, ledger) == (4, 4)


def test_completeness_missing_items_detected() -> None:
    ledger = rich_ledger()
    net = render(derive.net_balance(ledger))
    # Net stated, but itemization incomplete (INV-1002 missing), no mention
    # of the unapplied payment or the open credit by ID or amount.
    draft = f"Your balance is {net} on invoice INV-1001."
    assert completeness(draft, ledger) == (1, 4)


def test_completeness_net_balance_absent() -> None:
    ledger = rich_ledger()
    draft = (
        "You owe us money on INV-1001 and INV-1002; payment PMT-2001 and"
        " credit CM-3001 are on file."
    )
    assert completeness(draft, ledger) == (3, 4)


def test_completeness_na_items_excluded_from_denominator() -> None:
    ledger = settled_ledger()
    # No open invoice, no unapplied cash, no credit memo: only the net
    # balance item applies, and net is $0.00.
    assert derive.net_balance(ledger) == 0
    assert completeness("Your account is settled; the balance due is $0.00.", ledger) == (1, 1)
    assert completeness("Thank you for your payment.", ledger) == (0, 1)


def test_completeness_itemization_requires_every_open_invoice() -> None:
    ledger = rich_ledger()
    net = render(derive.net_balance(ledger))
    one_missing = f"Balance {net}; see INV-1001, payment PMT-2001, credit CM-3001."
    present, total = completeness(one_missing, ledger)
    assert (present, total) == (3, 4)


# ---------------------------------------------------------------------------
# build_score_event
# ---------------------------------------------------------------------------


def test_build_score_event_fills_every_field() -> None:
    ledger = rich_ledger()
    net = render(derive.net_balance(ledger))
    draft = (
        f"Net balance {net}. Invoices INV-1001 and INV-1002 are open; payment"
        " PMT-2001 is unapplied and credit CM-3001 remains available."
    )
    claims = [
        _claim("c1", ClaimType.C_SUM, net, CheckStatus.PASS),
        _claim("c2", ClaimType.C_EXIST, "INV-1001", CheckStatus.PASS),
        _claim("c3", ClaimType.C_AMT, "$999.99", CheckStatus.FAIL),
        _claim("c4", ClaimType.C_FUZZY, "thanks", CheckStatus.PASS),
    ]
    event = build_score_event(
        gen_id="G-1",
        scenario_id=ledger.scenario_id,
        pass_label="pass1",
        stage="first_pass",
        claims=claims,
        draft=draft,
        ledger=ledger,
        revision_count=1,
        terminal_action="human_gate",
        run_id="R-1",
    )
    assert event.event_type == "score"
    assert event.gen_id == "G-1"
    assert event.scenario_id == "S-T-01"
    assert event.pass_label == "pass1"
    assert event.stage == "first_pass"
    assert (event.grounding_checked, event.grounding_total) == grounding(claims) == (2, 3)
    assert (event.completeness_present, event.completeness_total) == (4, 4)
    assert event.revision_count == 1
    assert event.terminal_action == "human_gate"
    assert event.run_id == "R-1"
