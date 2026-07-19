"""Regression tests for the document/role binding family.

Each case is a sentence a prior version got wrong, in one of two directions:
a materially wrong claim that passed (false accept), or a true claim that
failed (false reject). The claim-level verdict is asserted directly, so a
regression shows up here regardless of the aggregate gate rows.
"""

from __future__ import annotations

import pytest

from balancecheck.checks.checks import run_code_checks
from balancecheck.contracts.models import ClaimType
from balancecheck.drafting.surface import extract_claims
from balancecheck.substrate.foundry import build_all

LEDGERS = {l.scenario_id: l for l in build_all()}


def _claims(sid: str, sentence: str):
    ledger = LEDGERS[sid]
    claims = extract_claims(sentence, ledger)
    run_code_checks(claims, ledger)
    return claims


def _status_of(claims, ctype, token_contains):
    for c in claims:
        if c.type is ctype and token_contains in c.token:
            return c.check_result.status.value if c.check_result else None
    return "MISSING"


# --- false accepts that must now FAIL --------------------------------------


def test_g23_wrong_per_invoice_amount_fails():
    # net is $2,150.00; INV-2987 open is $6,300.00. Stating the net as the
    # amount due on a specific invoice is wrong.
    claims = _claims("S-A-02", "The amount due on INV-2987 is $2,150.00.")
    assert _status_of(claims, ClaimType.C_AMT, "$2,150.00") == "fail"


def test_g12_role_swapped_amount_fails():
    # INV-3063 open is $5,200.00; $3,000.00 is the applied amount. Calling the
    # applied figure the open amount is materially misleading.
    claims = _claims("S-A-03", "Invoice INV-3063 is open for $3,000.00.")
    assert _status_of(claims, ClaimType.C_AMT, "$3,000.00") == "fail"


def test_g26_false_subtotal_equal_to_net_fails():
    # The two open invoices total $8,450.00, not the net $2,150.00.
    claims = _claims("S-A-02", "Invoices INV-2987 and INV-3014 together total $2,150.00.")
    assert _status_of(claims, ClaimType.C_SUM, "$2,150.00") == "fail"


def test_g24_shared_date_false_for_one_named_invoice_fails():
    # 2026-01-15 is INV-3063's issue date, not INV-3079's.
    claims = _claims("S-A-03", "Invoices INV-3079 and INV-3063 were both issued on 2026-01-15.")
    assert _status_of(claims, ClaimType.C_DATE, "2026-01-15") == "fail"


def test_g27_negative_amount_is_not_silently_made_positive():
    claims = _claims("S-A-02", "INV-2987 is open for -$6,300.00.")
    # the minus is kept, so the token does not match the positive ledger figure
    amt = next(c for c in claims if c.type is ClaimType.C_AMT and "6,300" in c.token)
    assert amt.token.startswith("-")
    assert amt.check_result.status.value == "fail"


# --- true claims that must now PASS ----------------------------------------


def test_g5_weaker_true_status_passes():
    # INV-2987 is overdue; "unpaid" and "outstanding" are true weaker words
    # that the status detector recognizes.
    for word in ("unpaid", "outstanding"):
        claims = _claims("S-A-02", f"INV-2987 is {word}.")
        assert _status_of(claims, ClaimType.C_STATUS, word) == "pass", word


def test_g5_falsely_stronger_status_still_fails():
    # INV-2959 is outstanding-not-yet-due; calling it overdue is falsely stronger.
    claims = _claims("S-A-01", "INV-2959 is overdue.")
    assert _status_of(claims, ClaimType.C_STATUS, "overdue") == "fail"


def test_g25_true_original_and_open_amounts_pass():
    claims = _claims(
        "S-A-03", "Invoice INV-3063 was issued for $8,200.00, and $5,200.00 is still due."
    )
    assert _status_of(claims, ClaimType.C_AMT, "$8,200.00") == "pass"
    assert _status_of(claims, ClaimType.C_AMT, "$5,200.00") == "pass"


def test_g4_two_status_sentence_does_not_cross_multiply():
    # Two invoices, two statuses; exactly two status claims, correctly bound.
    claims = _claims("S-A-02", "INV-2987 is overdue, while INV-3014 remains outstanding.")
    statuses = [(c.token, tuple(c.subject_ids)) for c in claims if c.type is ClaimType.C_STATUS]
    assert ("overdue", ("INV-2987",)) in statuses
    assert ("outstanding", ("INV-3014",)) in statuses
    assert len(statuses) == 2  # no invented "INV-2987 outstanding" / "INV-3014 overdue"


def test_g26_true_subtotal_passes():
    claims = _claims("S-A-02", "Invoices INV-2987 and INV-3014 together total $8,450.00.")
    assert _status_of(claims, ClaimType.C_SUM, "$8,450.00") == "pass"


def test_g16_true_received_transfer_passes():
    # $6,300.00 is a real payment (PMT-0265). "transfer" (remit-family) plus
    # "received" must classify as received, not escalate.
    claims = _claims("S-A-02", "We received your transfer of $6,300.00.")
    assert _status_of(claims, ClaimType.C_AMT, "$6,300.00") == "pass"


def test_account_total_passes_but_open_invoice_total_fails():
    # Stating the net as the account balance passes; stating the open-invoice
    # total as the account balance is the ignores-unapplied misreading.
    ok = _claims("S-A-02", "The balance due on your account is $2,150.00.")
    assert _status_of(ok, ClaimType.C_SUM, "$2,150.00") == "pass"
    bad = _claims("S-A-02", "Your total balance due is $8,450.00.")
    assert _status_of(bad, ClaimType.C_SUM, "$8,450.00") == "fail"
