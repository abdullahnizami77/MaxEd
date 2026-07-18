"""Recall tests for the two-stage claim extractor (invariant I11).

Table-driven over a small inline ledger: every detected quantitative span
must surface as a typed claim or as a claim pre-marked UNVERIFIABLE; nothing
the detector catches may be absent from the output, and nothing quantitative
is ever routed to the model.

Note on scope: the extractor supports the "USD 350" prefix form and the
"350 USD" suffix form in addition to $-forms, bare "N dollars", and
number-word amounts; loose document references are canonicalized with a
deterministic zero-padding match against the ledger ("payment 208" resolves
to PMT-0208 when exactly one ledger document matches).
"""

from __future__ import annotations

from datetime import date

import pytest

from balancecheck.contracts.models import (
    Application,
    CheckStatus,
    Claim,
    ClaimType,
    ClientInfo,
    CreditMemo,
    Invoice,
    Ledger,
    Payment,
)
from balancecheck.drafting.surface import extract_claims, parse_amount_token, parse_date_token

LEDGER = Ledger(
    scenario_id="S-X-01",
    pool="A",
    client=ClientInfo(id="CL-X", name="Extraction Test LLC", terms_days=30, as_of=date(2026, 3, 31)),
    invoices=[
        Invoice(id="INV-1012", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=240000),
        Invoice(id="INV-1020", date=date(2026, 3, 1), due=date(2026, 3, 31), amount_cents=150000),
    ],
    payments=[Payment(id="PMT-0208", date=date(2026, 3, 10), amount_cents=100000)],
    credit_memos=[CreditMemo(id="CM-0031", date=date(2026, 3, 15), amount_cents=15000)],
    applications=[Application(source_id="PMT-0208", target_invoice="INV-1012", amount_cents=100000)],
)


def _find(claims: list[Claim], ctype: ClaimType, token: str) -> list[Claim]:
    return [c for c in claims if c.type is ctype and c.token == token]


# ---------------------------------------------------------------------------
# Positive cases: each span must EXIST in the output with the right type
# ---------------------------------------------------------------------------

POSITIVE_CASES = [
    ("We show a charge of $2,400.00 on INV-1012.", ClaimType.C_AMT, "$2,400.00", False),
    ("INV-1012 is open for 2,400 dollars.", ClaimType.C_AMT, "2,400 dollars", False),
    ("The remaining charge comes to twelve hundred dollars.", ClaimType.C_AMT, "twelve hundred dollars", False),
    ("A processing fee of USD 350 was assessed.", ClaimType.C_AMT, "USD 350", False),
    ("Please review INV-1012 at your convenience.", ClaimType.C_EXIST, "INV-1012", False),
    ("Please review inv-1012 at your convenience.", ClaimType.C_EXIST, "INV-1012", False),
    ("Please review invoice 1012 at your convenience.", ClaimType.C_EXIST, "INV-1012", False),
    ("We have recorded payment 208 with thanks.", ClaimType.C_EXIST, "PMT-0208", False),
    ("Your credit memo 31 is on file.", ClaimType.C_EXIST, "CM-0031", False),
    ("Payment is expected by 2026-03-31.", ClaimType.C_DATE, "2026-03-31", False),
    ("Your payment arrived on March 3, 2026.", ClaimType.C_DATE, "March 3, 2026", False),
    ("You have covered 60 percent of INV-1012.", ClaimType.C_AMT, "60 percent", True),
    ("We can offset a quarter of the invoice.", ClaimType.C_AMT, "a quarter of the invoice", True),
    ("The balance due is $3,550.00.", ClaimType.C_SUM, "$3,550.00", False),
]


@pytest.mark.parametrize("text,ctype,token,unverifiable", POSITIVE_CASES)
def test_positive_span_is_never_dropped(
    text: str, ctype: ClaimType, token: str, unverifiable: bool
) -> None:
    claims = extract_claims(text, LEDGER)
    matches = _find(claims, ctype, token)
    assert matches, f"span {token!r} missing from claims: {[(c.type, c.token) for c in claims]}"
    claim = matches[0]
    assert claim.span, "every claim carries its verbatim sentence"
    if unverifiable:
        assert claim.check_result is not None
        assert claim.check_result.status is CheckStatus.UNVERIFIABLE
        assert claim.check_result.detail
    else:
        assert claim.check_result is None, "checkable claims arrive untyped for checks/ to fill"


def test_subject_resolution_single_document() -> None:
    claims = extract_claims("We show a charge of $2,400.00 on INV-1012.", LEDGER)
    (amt,) = _find(claims, ClaimType.C_AMT, "$2,400.00")
    assert amt.subject_id == "INV-1012"


def test_compound_sentence_yields_exist_amt_and_status() -> None:
    claims = extract_claims("INV-1012 for $2,400.00 remains outstanding.", LEDGER)
    assert [c.type for c in claims] == [ClaimType.C_EXIST, ClaimType.C_AMT, ClaimType.C_STATUS]
    assert [c.claim_id for c in claims] == ["c-001", "c-002", "c-003"]
    exist, amt, status = claims
    assert exist.token == "INV-1012"
    assert amt.token == "$2,400.00"
    assert amt.subject_id == "INV-1012"
    assert status.token == "outstanding"
    assert status.subject_id == "INV-1012"


def test_fuzzy_sentence_yields_one_fuzzy_claim() -> None:
    text = "As we discussed with your account manager, the fee is waived."
    claims = extract_claims(text, LEDGER)
    assert len(claims) == 1
    claim = claims[0]
    assert claim.type is ClaimType.C_FUZZY
    assert claim.span == text
    assert claim.token == ""


def test_status_needs_a_document_reference() -> None:
    # "paid" without any document in the sentence is not a status claim.
    claims = extract_claims("Everything has been paid.", LEDGER)
    assert not [c for c in claims if c.type is ClaimType.C_STATUS]


def test_exist_dedup_one_claim_per_distinct_id() -> None:
    text = "INV-1012 is listed below. INV-1012 also appears in invoice 1012."
    claims = extract_claims(text, LEDGER)
    exists = [c for c in claims if c.type is ClaimType.C_EXIST]
    assert len(exists) == 1
    assert exists[0].token == "INV-1012"


def test_claim_prefix_and_id_format() -> None:
    claims = extract_claims("The balance due is $3,550.00 as of 2026-03-31.", LEDGER, claim_prefix="x")
    assert claims, "expected claims from a quantitative sentence"
    assert claims[0].claim_id == "x-001"
    for n, c in enumerate(claims, start=1):
        assert c.claim_id == f"x-{n:03d}"


# ---------------------------------------------------------------------------
# Negative cases: sentences with no quantitative or fuzzy content
# ---------------------------------------------------------------------------

NEGATIVE_CASES = [
    "Thank you for your business.",
    "We appreciate your patience and look forward to your response.",
    "Please reach out if anything here needs a second look.",
]


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_negative_sentences_yield_nothing(text: str) -> None:
    assert extract_claims(text, LEDGER) == []


# ---------------------------------------------------------------------------
# The I11 property over a multi-sentence draft: everything detected appears
# ---------------------------------------------------------------------------

I11_DRAFT = (
    "Your current balance is $3,550.00.\n"
    "INV-1012 for $2,400.00 remains outstanding, and invoice 1020 is open for $1,500.00.\n"
    "You have already covered 40 percent of the total.\n"
    "We can waive a third of the balance.\n"
    "Your account manager approved a discount on 3/15/2026."
)

I11_EXPECTED = [
    (ClaimType.C_SUM, "$3,550.00", False),
    (ClaimType.C_EXIST, "INV-1012", False),
    (ClaimType.C_EXIST, "INV-1020", False),
    (ClaimType.C_AMT, "$2,400.00", False),
    (ClaimType.C_AMT, "$1,500.00", False),
    (ClaimType.C_STATUS, "outstanding", False),
    (ClaimType.C_SUM, "40 percent", True),
    (ClaimType.C_SUM, "a third of the balance", True),
    (ClaimType.C_DATE, "3/15/2026", False),
]


def test_i11_nothing_detected_is_absent_from_output() -> None:
    claims = extract_claims(I11_DRAFT, LEDGER)
    for ctype, token, unverifiable in I11_EXPECTED:
        matches = _find(claims, ctype, token)
        assert matches, f"I11 violated: detected span {token!r} missing from output"
        if unverifiable:
            assert matches[0].check_result is not None
            assert matches[0].check_result.status is CheckStatus.UNVERIFIABLE
    fuzzy = [c for c in claims if c.type is ClaimType.C_FUZZY]
    assert len(fuzzy) == 2, "the waive sentence and the account-manager sentence each escalate"
    ids = [c.claim_id for c in claims]
    assert len(ids) == len(set(ids))
    assert ids == [f"c-{n:03d}" for n in range(1, len(ids) + 1)]


def test_unverifiable_claims_are_never_model_routed() -> None:
    # Percent and fraction spans arrive with their check_result already set,
    # so the gate escalates them without ever consulting the fuzzy verifier.
    claims = extract_claims(I11_DRAFT, LEDGER)
    for c in claims:
        if c.check_result is not None:
            assert c.check_result.status is CheckStatus.UNVERIFIABLE
            assert c.type is not ClaimType.C_FUZZY


# ---------------------------------------------------------------------------
# parse_amount_token: the one parse point checks/ reuses
# ---------------------------------------------------------------------------


def test_parse_amount_token_digit_forms() -> None:
    assert parse_amount_token("$2,400.00") == 240000
    assert parse_amount_token("2,400 dollars") == 240000
    assert parse_amount_token("USD 350") == 35000
    assert parse_amount_token("350 USD") == 35000
    assert parse_amount_token("$35.50") == 3550


def test_parse_amount_token_word_forms() -> None:
    assert parse_amount_token("twelve hundred dollars") == 120000
    assert parse_amount_token("two thousand four hundred dollars") == 240000
    assert parse_amount_token("ninety dollars") == 9000


def test_parse_amount_token_rejects_unresolvable() -> None:
    for bad in ("60 percent", "60%", "a quarter of the invoice", "half of the balance", "gibberish"):
        with pytest.raises(ValueError):
            parse_amount_token(bad)


def test_parse_date_token_forms() -> None:
    assert parse_date_token("2026-03-31") == date(2026, 3, 31)
    assert parse_date_token("March 3, 2026") == date(2026, 3, 3)
    assert parse_date_token("3/15/2026") == date(2026, 3, 15)
    with pytest.raises(ValueError):
        parse_date_token("sometime soon")
