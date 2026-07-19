"""One test per decision-matrix row (plan section 8), plus severity ordering,
the revision budgets at their boundaries, and the capability-gap mapping each
terminal row produces.

Hermetic: ledgers and claims are hand-built; no model, no network.
"""

from __future__ import annotations

from datetime import date

from balancecheck.contracts.models import (
    Application,
    CapabilityGapEvent,
    CheckResult,
    CheckStatus,
    Claim,
    ClaimType,
    ClientInfo,
    GapCategory,
    GateAction,
    Invoice,
    Ledger,
    Payment,
)
from balancecheck.gate.gaps import aggregate_gaps, derive_gap
from balancecheck.gate.policy import build_correction, decide, precheck_abstain


def clean_ledger() -> Ledger:
    """One open invoice ($2,400.00), one paid, one open credit ($150.00);
    net balance $2,250.00; no ambiguity."""
    return Ledger(
        scenario_id="T-CLEAN",
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
            ),
            Invoice(
                id="INV-1013",
                date=date(2026, 2, 1),
                due=date(2026, 3, 3),
                amount_cents=120000,
            ),
        ],
        payments=[
            Payment(id="PMT-0208", date=date(2026, 3, 5), amount_cents=120000)
        ],
        credit_memos=[],
        applications=[
            Application(
                source_id="PMT-0208", target_invoice="INV-1013", amount_cents=120000
            )
        ],
    )


def ambiguous_ledger() -> Ledger:
    """Two open invoices at the same amount and one unapplied payment that
    matches both: the records cannot say where the money goes."""
    return Ledger(
        scenario_id="T-AMBIG",
        pool="A",
        structure_labels=[],
        client=ClientInfo(
            id="CL-T", name="Test Client Co.", terms_days=30, as_of=date(2026, 3, 31)
        ),
        invoices=[
            Invoice(
                id="INV-2001",
                date=date(2026, 3, 1),
                due=date(2026, 3, 31),
                amount_cents=240000,
            ),
            Invoice(
                id="INV-2002",
                date=date(2026, 3, 2),
                due=date(2026, 4, 1),
                amount_cents=240000,
            ),
        ],
        payments=[
            Payment(id="PMT-0300", date=date(2026, 3, 20), amount_cents=240000)
        ],
        credit_memos=[],
        applications=[],
    )


def claim_with(
    cid: str,
    ctype: ClaimType,
    status: CheckStatus,
    span: str,
    token: str = "",
    subject: str = "",
    expected: str = "",
    actual: str = "",
    detail: str = "",
    cited_records: list[str] | None = None,
) -> Claim:
    return Claim(
        claim_id=cid,
        type=ctype,
        span=span,
        token=token,
        subject_id=subject,
        check_result=CheckResult(
            status=status,
            expected=expected,
            actual=actual,
            detail=detail,
            cited_records=cited_records or []
        ),
    )


def passing_claim(cid: str = "p-001") -> Claim:
    # A clean draft's passing total cites NET, which the missing-net policy
    # row (adversarial-review hardening) keys on.
    return claim_with(
        cid,
        ClaimType.C_SUM,
        CheckStatus.PASS,
        "Your balance is $2,250.00.",
        "$2,250.00",
        expected="$2,250.00",
        actual="$2,250.00",
        cited_records=["NET"],
    )


def exist_fail(cid: str = "x-001") -> Claim:
    return claim_with(
        cid,
        ClaimType.C_EXIST,
        CheckStatus.FAIL,
        "Please see invoice INV-9999.",
        "INV-9999",
        detail="no such document in the ledger",
    )


def unverifiable(cid: str = "u-001") -> Claim:
    return claim_with(
        cid,
        ClaimType.C_AMT,
        CheckStatus.UNVERIFIABLE,
        "Please remit a quarter of the invoice.",
        "a quarter of the invoice",
        actual="a quarter of the invoice",
        detail="detected amount span could not be resolved to cents",
    )


def sum_fail(cid: str = "s-001") -> Claim:
    return claim_with(
        cid,
        ClaimType.C_SUM,
        CheckStatus.FAIL,
        "Your balance is $2,400.00.",
        "$2,400.00",
        expected="$2,250.00",
        actual="$2,400.00",
        detail="states the open-invoice total and ignores unapplied cash or credits",
    )


def amt_fail(cid: str = "a-001") -> Claim:
    return claim_with(
        cid,
        ClaimType.C_AMT,
        CheckStatus.FAIL,
        "INV-1012 is $2,040.00.",
        "$2,040.00",
        subject="INV-1012",
        expected="$2,400.00",
        actual="$2,040.00",
        detail="amount does not match INV-1012",
    )


def fuzzy_unsupported(cid: str = "z-001") -> Claim:
    return claim_with(
        cid,
        ClaimType.C_FUZZY,
        CheckStatus.UNSUPPORTED,
        "Your account manager approved this adjustment.",
        detail="no record of an approval",
    )


def fuzzy_cannot(cid: str = "q-001") -> Claim:
    return claim_with(
        cid,
        ClaimType.C_FUZZY,
        CheckStatus.CANNOT_DETERMINE,
        "As discussed by phone, your terms were extended.",
        detail="records are silent",
    )


def fresh_hashes(n: int = 1) -> list[str]:
    return [f"hash-{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# Row 1: fabricated reference escalates immediately
# ---------------------------------------------------------------------------


def test_row1_c_exist_fail_escalates():
    decision = decide(
        [passing_claim(), exist_fail()], clean_ledger(), 0, fresh_hashes()
    )
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "fabricated document reference"
    assert "Please see invoice INV-9999." in decision.payload
    assert "INV-9999" in decision.payload
    gap = derive_gap(decision, clean_ledger(), "g-1")
    assert gap is not None
    assert gap.missing is GapCategory.DOCUMENT_ABSENT
    assert gap.would_need == "drafter constrained to the document set"
    assert gap.resolvable_by == "tool"


# ---------------------------------------------------------------------------
# Row 2: unverifiable quantitative claim escalates
# ---------------------------------------------------------------------------


def test_row2_unverifiable_escalates_with_spans():
    decision = decide(
        [passing_claim(), unverifiable()], clean_ledger(), 0, fresh_hashes()
    )
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "unverifiable quantitative claim"
    assert "Please remit a quarter of the invoice." in decision.payload
    gap = derive_gap(decision, clean_ledger(), "g-2")
    assert gap is not None
    assert gap.missing is GapCategory.UNVERIFIABLE_QUANT
    assert gap.would_need == "a claim normalizer for non-canonical amount phrasing"
    assert gap.resolvable_by == "tool"


def test_row2_a_claim_that_was_never_checked_counts_as_unverifiable():
    unchecked = Claim(
        claim_id="n-001",
        type=ClaimType.C_AMT,
        span="An amount of $5.00 nobody checked.",
        token="$5.00",
    )
    decision = decide([passing_claim(), unchecked], clean_ledger(), 0, fresh_hashes())
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "unverifiable quantitative claim"


# ---------------------------------------------------------------------------
# Row 3: correctable code failures revise, then exhaust the budget
# ---------------------------------------------------------------------------


def test_row3_code_fail_revises_below_budget_with_correction():
    for revision_index in (0, 1):
        decision = decide(
            [passing_claim(), sum_fail(), amt_fail()],
            clean_ledger(),
            revision_index,
            fresh_hashes(revision_index + 1),
        )
        assert decision.action is GateAction.REVISE, revision_index
        correction = decision.payload
        assert "$2,250.00" in correction, "expected rendering present"
        assert "$2,400.00" in correction, "actual rendering present"
        assert "$2,040.00" in correction
        assert "INV-1012" in correction
        assert (
            "The ledger shows $2,250.00 for the total balance due; "
            "your draft says $2,400.00." in correction
        )


def test_row3_budget_exhausted_escalates_at_revision_index_2():
    decision = decide(
        [passing_claim(), sum_fail()], clean_ledger(), 2, fresh_hashes(3)
    )
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "revision budget exhausted"
    gap = derive_gap(decision, clean_ledger(), "g-3")
    assert gap is not None
    assert gap.missing is GapCategory.OTHER
    assert gap.resolvable_by == "human"


def test_build_correction_carries_expected_and_actual():
    text = build_correction([sum_fail(), amt_fail()])
    assert "The ledger shows $2,250.00 for the total balance due;" in text
    assert "your draft says $2,400.00." in text
    assert "The ledger shows $2,400.00 for INV-1012;" in text
    assert "your draft says $2,040.00." in text


# ---------------------------------------------------------------------------
# Row 4: fuzzy unsupported revises exactly once
# ---------------------------------------------------------------------------


def test_row4_fuzzy_unsupported_revises_once_then_escalates():
    revise = decide(
        [passing_claim(), fuzzy_unsupported()], clean_ledger(), 0, fresh_hashes()
    )
    assert revise.action is GateAction.REVISE

    escalate = decide(
        [passing_claim(), fuzzy_unsupported()], clean_ledger(), 1, fresh_hashes(2)
    )
    assert escalate.action is GateAction.ESCALATE
    assert "Your account manager approved this adjustment." in escalate.payload
    gap = derive_gap(escalate, clean_ledger(), "g-4")
    assert gap is not None
    assert gap.missing is GapCategory.OTHER


# ---------------------------------------------------------------------------
# Row 5: fuzzy cannot-determine escalates with what would resolve it
# ---------------------------------------------------------------------------


def test_row5_fuzzy_cannot_determine_escalates():
    decision = decide(
        [passing_claim(), fuzzy_cannot()], clean_ledger(), 0, fresh_hashes()
    )
    assert decision.action is GateAction.ESCALATE
    assert "As discussed by phone, your terms were extended." in decision.payload
    assert "would resolve" in decision.payload
    gap = derive_gap(decision, clean_ledger(), "g-5")
    assert gap is not None
    assert gap.missing is GapCategory.AMBIGUOUS_REFERENCE
    assert gap.resolvable_by == "human"


# ---------------------------------------------------------------------------
# Row 6: ledger ambiguity abstains, before drafting and as a backstop
# ---------------------------------------------------------------------------


def test_row6_ambiguous_allocation_abstains_with_candidates():
    ledger = ambiguous_ledger()
    decision = decide([passing_claim()], ledger, 0, fresh_hashes())
    assert decision.action is GateAction.ABSTAIN
    assert "PMT-0300" in decision.payload
    assert "INV-2001" in decision.payload
    assert "INV-2002" in decision.payload
    gap = derive_gap(decision, ledger, "g-6")
    assert gap is not None
    assert gap.missing is GapCategory.ALLOCATION_REFERENCE
    assert gap.would_need == "remittance advice tying the payment to an invoice"
    assert gap.resolvable_by == "human"


def test_precheck_abstain_fires_before_drafting_and_only_on_ambiguity():
    pre = precheck_abstain(ambiguous_ledger())
    assert pre is not None
    assert pre.action is GateAction.ABSTAIN
    assert "PMT-0300" in pre.payload
    assert precheck_abstain(clean_ledger()) is None


# ---------------------------------------------------------------------------
# Row 7: all pass goes to the human gate
# ---------------------------------------------------------------------------


def test_row7_all_pass_reaches_human_gate_with_empty_findings():
    decision = decide(
        [passing_claim("p-1"), passing_claim("p-2")], clean_ledger(), 0, fresh_hashes()
    )
    assert decision.action is GateAction.HUMAN_GATE
    assert decision.reason == "all checks passed"
    assert decision.findings == []
    assert derive_gap(decision, clean_ledger(), "g-7") is None


def test_revise_produces_no_gap():
    decision = decide([sum_fail()], clean_ledger(), 0, fresh_hashes())
    assert decision.action is GateAction.REVISE
    assert derive_gap(decision, clean_ledger(), "g-8") is None


# ---------------------------------------------------------------------------
# Severity ordering: first match wins
# ---------------------------------------------------------------------------


def test_severity_order_fabrication_beats_everything():
    decision = decide(
        [exist_fail(), unverifiable(), sum_fail(), fuzzy_unsupported(), fuzzy_cannot()],
        ambiguous_ledger(),
        0,
        fresh_hashes(),
    )
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "fabricated document reference"


def test_severity_order_unverifiable_beats_code_fail():
    decision = decide(
        [unverifiable(), sum_fail()], clean_ledger(), 0, fresh_hashes()
    )
    assert decision.reason == "unverifiable quantitative claim"


def test_severity_order_code_fail_beats_fuzzy_findings():
    decision = decide(
        [sum_fail(), fuzzy_unsupported(), fuzzy_cannot()],
        clean_ledger(),
        0,
        fresh_hashes(),
    )
    assert decision.action is GateAction.REVISE
    kinds = {f.kind for f in decision.findings}
    assert kinds == {"c_sum_fail"}


# ---------------------------------------------------------------------------
# Gap aggregation
# ---------------------------------------------------------------------------


def test_aggregate_gaps_folds_by_category():
    ledger = ambiguous_ledger()
    events: list[CapabilityGapEvent] = []
    for gen_id in ("g-10", "g-11"):
        gap = derive_gap(decide([], ledger, 0, fresh_hashes()), ledger, gen_id)
        assert gap is not None
        events.append(gap)
    fabricated = derive_gap(
        decide([exist_fail()], clean_ledger(), 0, fresh_hashes()), clean_ledger(), "g-12"
    )
    assert fabricated is not None
    events.append(fabricated)
    agg = aggregate_gaps(events)
    assert agg == {
        "allocation_reference": ["g-10", "g-11"],
        "document_absent": ["g-12"],
    }
