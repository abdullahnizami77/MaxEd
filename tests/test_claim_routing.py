"""Invariant I3: every claim type routes to a checker, code types never touch
a model, and the one model-checked type flows through the isolated fuzzy path
with its citations re-verified by code.

Hermetic: ledgers are built inline, fuzzy calls run against a stub-mode
client with a temp stub file and temp log path. Zero network.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from balancecheck.checks.checks import (
    MODEL_CHECKED_TYPES,
    REGISTRY,
    check_all,
    check_fuzzy,
    run_code_checks,
)
from balancecheck.config import Config
from balancecheck.contracts.models import (
    CODE_CHECKED_TYPES,
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
from balancecheck.model_client import ModelClient, ModelResponse


def make_ledger() -> Ledger:
    """INV-1012 open $2,400.00; INV-1013 paid in full by PMT-0208; CM-0031
    open credit $150.00. Net balance: $2,250.00."""
    return Ledger(
        scenario_id="T-ROUTE",
        pool="A",
        structure_labels=[],
        client=ClientInfo(
            id="CL-T", name="Test Client Co.", terms_days=30, as_of=date(2026, 3, 31)
        ),
        invoices=[
            Invoice(
                id="INV-1012",
                date=date(2026, 3, 1),
                due=date(2026, 3, 31),
                amount_cents=240000,
                memo="design retainer",
            ),
            Invoice(
                id="INV-1013",
                date=date(2026, 2, 1),
                due=date(2026, 3, 3),
                amount_cents=120000,
                memo="print run",
            ),
        ],
        payments=[
            Payment(
                id="PMT-0208",
                date=date(2026, 3, 5),
                amount_cents=120000,
                method="ach",
                reference="ACH-88213",
            )
        ],
        credit_memos=[
            CreditMemo(
                id="CM-0031",
                date=date(2026, 3, 10),
                amount_cents=15000,
                reason="goodwill credit",
            )
        ],
        applications=[
            Application(
                source_id="PMT-0208", target_invoice="INV-1013", amount_cents=120000
            )
        ],
    )


def make_claim(
    cid: str,
    ctype: ClaimType,
    span: str,
    token: str,
    subject: str = "",
    result: CheckResult | None = None,
) -> Claim:
    return Claim(
        claim_id=cid,
        type=ctype,
        span=span,
        token=token,
        subject_id=subject,
        check_result=result,
    )


class PoisonClient:
    """Any attribute access at all is a routing violation."""

    def __getattribute__(self, name: str):
        raise AssertionError(
            f"code checks must never touch the model client (accessed {name!r})"
        )


def stub_client(tmp_path: Path, stubs: dict[str, dict]) -> ModelClient:
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps(stubs))
    cfg = Config(
        mode="stub", log_path=tmp_path / "events.jsonl", stub_file=stub_file
    )
    return ModelClient(cfg=cfg, run_id="t")


def verdict_stub(verdict: str, cited: list[str], reason: str = "because") -> dict:
    return {
        "text": json.dumps(
            {"verdict": verdict, "cited_record_ids": cited, "reason": reason}
        )
    }


# ---------------------------------------------------------------------------
# The routing table
# ---------------------------------------------------------------------------


def test_every_claim_type_is_in_the_routing_table():
    assert set(REGISTRY) == set(ClaimType)


def test_code_and_model_partition_is_total():
    assert CODE_CHECKED_TYPES | MODEL_CHECKED_TYPES == set(ClaimType)
    assert not CODE_CHECKED_TYPES & MODEL_CHECKED_TYPES


def test_code_types_resolve_with_a_client_that_must_never_be_touched():
    ledger = make_ledger()
    claims = [
        make_claim("r-001", ClaimType.C_EXIST, "See INV-1012.", "INV-1012"),
        make_claim(
            "r-002", ClaimType.C_AMT, "INV-1012 is $2,400.00.", "$2,400.00", "INV-1012"
        ),
        make_claim("r-003", ClaimType.C_SUM, "Your balance is $2,250.00.", "$2,250.00"),
        make_claim(
            "r-004",
            ClaimType.C_STATUS,
            "INV-1012 is outstanding.",
            "outstanding",
            "INV-1012",
        ),
        make_claim(
            "r-005", ClaimType.C_DATE, "INV-1012 dated 2026-03-01.", "2026-03-01", "INV-1012"
        ),
    ]
    checked = check_all(claims, ledger, PoisonClient())
    for claim in checked:
        assert claim.check_result is not None, claim.claim_id
        assert claim.check_result.status is CheckStatus.PASS, claim.claim_id


def test_run_code_checks_skips_preset_unverifiable_results():
    ledger = make_ledger()
    preset = CheckResult(
        status=CheckStatus.UNVERIFIABLE,
        actual="a quarter of the invoice",
        detail="detected amount span could not be resolved to cents",
    )
    claim = make_claim(
        "r-010",
        ClaimType.C_AMT,
        "Please remit a quarter of the invoice.",
        "a quarter of the invoice",
        result=preset,
    )
    run_code_checks([claim], ledger)
    assert claim.check_result is preset, "a pre-marked claim is never overwritten"
    assert claim.check_result.status is CheckStatus.UNVERIFIABLE


# ---------------------------------------------------------------------------
# Code checker behavior (the layer trust rides on)
# ---------------------------------------------------------------------------


def test_c_exist_fail_is_a_fabricated_reference():
    ledger = make_ledger()
    claim = make_claim("r-020", ClaimType.C_EXIST, "See INV-9999.", "INV-9999")
    run_code_checks([claim], ledger)
    assert claim.check_result is not None
    assert claim.check_result.status is CheckStatus.FAIL
    assert claim.check_result.detail == "no such document in the ledger"


def test_c_amt_subject_accepts_amount_or_open_and_renders_both_on_fail():
    ledger = make_ledger()
    ok_amount = make_claim(
        "r-030", ClaimType.C_AMT, "INV-1013 was $1,200.00.", "$1,200.00", "INV-1013"
    )
    ok_open = make_claim(
        "r-031", ClaimType.C_AMT, "INV-1012 has $2,400.00 open.", "$2,400.00", "INV-1012"
    )
    bad = make_claim(
        "r-032", ClaimType.C_AMT, "INV-1013 was $1,250.00.", "$1,250.00", "INV-1013"
    )
    run_code_checks([ok_amount, ok_open, bad], ledger)
    assert ok_amount.check_result.status is CheckStatus.PASS
    assert ok_amount.check_result.cited_records == ["INV-1013"]
    assert ok_open.check_result.status is CheckStatus.PASS
    assert bad.check_result.status is CheckStatus.FAIL
    assert "$1,200.00" in bad.check_result.expected
    assert "$0.00" in bad.check_result.expected, "open amount offered too"
    assert bad.check_result.actual == "$1,250.00"


def test_c_amt_without_subject_is_context_strict():
    """Hardened after the adversarial red team: a bare amount matching some
    unrelated ledger row is not support. Net balance passes; received-context
    amounts must match a payment; everything else escalates as unverifiable."""
    ledger = make_ledger()
    net = make_claim("r-040", ClaimType.C_AMT, "That leaves $2,250.00.", "$2,250.00")
    credit = make_claim("r-041", ClaimType.C_AMT, "A credit of $150.00.", "$150.00")
    word_form = make_claim(
        "r-042", ClaimType.C_AMT, "We received 1,200 dollars.", "1,200 dollars"
    )
    received_wrong = make_claim(
        "r-044", ClaimType.C_AMT, "We received $2,250.00 from you.", "$2,250.00"
    )
    remit_wrong = make_claim(
        "r-045", ClaimType.C_AMT, "Please remit $1,200.00 today.", "$1,200.00"
    )
    nothing = make_claim("r-043", ClaimType.C_AMT, "A charge of $9.99.", "$9.99")
    run_code_checks(
        [net, credit, word_form, received_wrong, remit_wrong, nothing], ledger
    )
    assert net.check_result.status is CheckStatus.PASS
    assert "NET" in net.check_result.cited_records
    # A document figure in a plain sentence is no longer blessed by
    # coincidence: it escalates for a human to attribute.
    assert credit.check_result.status is CheckStatus.UNVERIFIABLE
    assert word_form.check_result.status is CheckStatus.PASS, (
        "received-context amount matching a real payment passes"
    )
    assert "PMT-0208" in word_form.check_result.cited_records
    assert received_wrong.check_result.status is CheckStatus.FAIL, (
        "received-context amount matching no payment is materially false"
    )
    assert remit_wrong.check_result.status is CheckStatus.FAIL, (
        "a payment request for anything but the net balance is materially false"
    )
    assert nothing.check_result.status is CheckStatus.UNVERIFIABLE


def test_c_sum_flags_the_open_invoice_total_misreading():
    ledger = make_ledger()
    good = make_claim("r-050", ClaimType.C_SUM, "You owe $2,250.00.", "$2,250.00")
    tier2 = make_claim("r-051", ClaimType.C_SUM, "You owe $2,400.00.", "$2,400.00")
    other = make_claim("r-052", ClaimType.C_SUM, "You owe $2,000.00.", "$2,000.00")
    run_code_checks([good, tier2, other], ledger)
    assert good.check_result.status is CheckStatus.PASS
    assert good.check_result.cited_records == ["NET"]
    assert tier2.check_result.status is CheckStatus.FAIL
    assert (
        tier2.check_result.detail
        == "states the open-invoice total and ignores unapplied cash or credits"
    )
    assert tier2.check_result.expected == "$2,250.00"
    assert tier2.check_result.actual == "$2,400.00"
    assert other.check_result.status is CheckStatus.FAIL
    assert other.check_result.detail == "does not match the computed balance"


def test_c_status_maps_words_and_requires_a_subject():
    ledger = make_ledger()
    settled = make_claim(
        "r-060", ClaimType.C_STATUS, "INV-1013 is settled.", "settled", "INV-1013"
    )
    wrong = make_claim(
        "r-061", ClaimType.C_STATUS, "INV-1012 is past due.", "past due", "INV-1012"
    )
    orphan = make_claim("r-062", ClaimType.C_STATUS, "It is all paid.", "paid")
    run_code_checks([settled, wrong, orphan], ledger)
    assert settled.check_result.status is CheckStatus.PASS
    assert settled.check_result.expected == "paid"
    assert wrong.check_result.status is CheckStatus.FAIL
    assert wrong.check_result.expected == "outstanding"
    assert wrong.check_result.cited_records == ["INV-1012"]
    # A status with no resolvable document is unverifiable (escalates), not
    # provably false: hardened after the adversarial review.
    assert orphan.check_result.status is CheckStatus.UNVERIFIABLE
    assert orphan.check_result.detail == "status claim with no resolvable document"


def test_c_date_matches_first_and_condemns_unmatched_future_dates():
    ledger = make_ledger()
    doc_date = make_claim(
        "r-070", ClaimType.C_DATE, "INV-1013 dated 2026-02-01.", "2026-02-01", "INV-1013"
    )
    slash = make_claim("r-071", ClaimType.C_DATE, "Received on 3/5/2026.", "3/5/2026")
    month_name = make_claim(
        "r-072", ClaimType.C_DATE, "As of March 31, 2026.", "March 31, 2026"
    )
    future = make_claim("r-073", ClaimType.C_DATE, "Due by 2026-04-15.", "2026-04-15")
    stale = make_claim("r-074", ClaimType.C_DATE, "Sent 2026-01-15.", "2026-01-15")
    garbage = make_claim(
        "r-075", ClaimType.C_DATE, "Due the fifth of March.", "the fifth of March"
    )
    run_code_checks([doc_date, slash, month_name, future, stale, garbage], ledger)
    assert doc_date.check_result.status is CheckStatus.PASS
    assert doc_date.check_result.cited_records == ["INV-1013"]
    assert slash.check_result.status is CheckStatus.PASS
    assert "PMT-0208" in slash.check_result.cited_records
    assert month_name.check_result.status is CheckStatus.PASS
    assert "AS_OF" in month_name.check_result.cited_records
    assert future.check_result.status is CheckStatus.FAIL
    assert future.check_result.detail == "date is after the statement as_of"
    assert stale.check_result.status is CheckStatus.FAIL
    assert stale.check_result.detail != "date is after the statement as_of"
    assert garbage.check_result.status is CheckStatus.UNVERIFIABLE


def test_c_date_passes_a_ledger_due_date_beyond_as_of():
    """A due date later than as_of that the ledger itself states must pass;
    the as_of rule condemns only dates the ledger does not carry."""
    ledger = make_ledger().model_copy(deep=True)
    ledger.invoices[0].due = date(2026, 4, 30)
    claim = make_claim(
        "r-080", ClaimType.C_DATE, "INV-1012 is due 2026-04-30.", "2026-04-30", "INV-1012"
    )
    run_code_checks([claim], ledger)
    assert claim.check_result.status is CheckStatus.PASS


# ---------------------------------------------------------------------------
# The model path (C-FUZZY): stub client, verdict mapping, citation policing
# ---------------------------------------------------------------------------


def test_fuzzy_routes_to_the_model_and_maps_unsupported(tmp_path):
    ledger = make_ledger()
    claim = make_claim(
        "f-001",
        ClaimType.C_FUZZY,
        "Your account manager approved this adjustment.",
        "",
    )
    client = stub_client(
        tmp_path,
        {"t:fuzzy:f-001": verdict_stub("unsupported", [], "no record of an approval")},
    )
    check_all([claim], ledger, client, stub_key_prefix="t")
    assert claim.check_result is not None
    assert claim.check_result.status is CheckStatus.UNSUPPORTED
    assert claim.check_result.detail == "no record of an approval"
    assert client.call_count == 1, "the fuzzy claim made exactly one model call"


def test_fuzzy_supported_with_valid_citation_maps_to_pass(tmp_path):
    ledger = make_ledger()
    claim = make_claim(
        "f-002", ClaimType.C_FUZZY, "We issued a goodwill credit for the delay.", ""
    )
    client = stub_client(
        tmp_path,
        {"t:fuzzy:f-002": verdict_stub("supported", ["CM-0031"], "credit memo reason")},
    )
    check_all([claim], ledger, client, stub_key_prefix="t")
    assert claim.check_result.status is CheckStatus.PASS
    assert claim.check_result.cited_records == ["CM-0031"]


def test_fuzzy_cannot_determine_maps_through(tmp_path):
    ledger = make_ledger()
    claim = make_claim(
        "f-003", ClaimType.C_FUZZY, "As agreed on our last call, terms are extended.", ""
    )
    client = stub_client(
        tmp_path,
        {"t:fuzzy:f-003": verdict_stub("cannot_determine", [], "records are silent")},
    )
    check_all([claim], ledger, client, stub_key_prefix="t")
    assert claim.check_result.status is CheckStatus.CANNOT_DETERMINE


def test_fuzzy_nonexistent_citation_is_downgraded_by_code(tmp_path):
    ledger = make_ledger()
    claim = make_claim(
        "f-004", ClaimType.C_FUZZY, "The waiver was recorded on your account.", ""
    )
    client = stub_client(
        tmp_path,
        {"t:fuzzy:f-004": verdict_stub("supported", ["INV-7777", "CM-0031"], "sure")},
    )
    check_all([claim], ledger, client, stub_key_prefix="t")
    assert claim.check_result.status is CheckStatus.CANNOT_DETERMINE
    assert claim.check_result.detail.startswith("verifier cited a nonexistent record")
    assert "INV-7777" in claim.check_result.detail
    assert claim.check_result.cited_records == ["CM-0031"], "only real records remain"


def test_fuzzy_failed_model_call_is_cannot_determine():
    ledger = make_ledger()
    claim = make_claim("f-005", ClaimType.C_FUZZY, "We spoke about a payment plan.", "")

    class FailingClient:
        def complete(self, *args, **kwargs) -> ModelResponse:
            return ModelResponse(text="", ok=False, error="endpoint melted")

    result = check_fuzzy(claim, ledger, FailingClient(), "t")
    assert result.status is CheckStatus.CANNOT_DETERMINE
    assert "endpoint melted" in result.detail


def test_fuzzy_with_preset_result_is_not_recalled(tmp_path):
    ledger = make_ledger()
    preset = CheckResult(status=CheckStatus.UNSUPPORTED, detail="already verified")
    claim = make_claim(
        "f-006", ClaimType.C_FUZZY, "We agreed to waive the fee.", "", result=preset
    )
    client = stub_client(tmp_path, {})
    check_all([claim], ledger, client, stub_key_prefix="t")
    assert claim.check_result is preset
    assert client.call_count == 0


def test_fuzzy_missing_stub_key_raises_not_silently_passes(tmp_path):
    ledger = make_ledger()
    claim = make_claim("f-007", ClaimType.C_FUZZY, "A promise was made.", "")
    client = stub_client(tmp_path, {})
    with pytest.raises(KeyError):
        check_all([claim], ledger, client, stub_key_prefix="t")
