"""The oscillation guard: a draft hash that repeats an earlier one means the
model is cycling, and any REVISE converts to ESCALATE "revision loop". The
guard touches only REVISE; decisions that already terminate keep their own
reasons.

Hermetic: hand-built ledger and claims, no model, no network.
"""

from __future__ import annotations

from datetime import date

from balancecheck.contracts.models import (
    Application,
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
from balancecheck.gate.gaps import derive_gap
from balancecheck.gate.policy import decide


def make_ledger() -> Ledger:
    return Ledger(
        scenario_id="T-OSC",
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
            )
        ],
        payments=[Payment(id="PMT-0208", date=date(2026, 3, 5), amount_cents=90000)],
        credit_memos=[],
        applications=[
            Application(
                source_id="PMT-0208", target_invoice="INV-1012", amount_cents=90000
            )
        ],
    )


def sum_fail() -> Claim:
    return Claim(
        claim_id="s-1",
        type=ClaimType.C_SUM,
        span="You owe $2,400.00.",
        token="$2,400.00",
        check_result=CheckResult(
            status=CheckStatus.FAIL,
            expected="$1,500.00",
            actual="$2,400.00",
            detail="does not match the computed balance",
        ),
    )


def fuzzy_unsupported() -> Claim:
    return Claim(
        claim_id="z-1",
        type=ClaimType.C_FUZZY,
        span="We agreed to waive the late fee.",
        token="",
        check_result=CheckResult(
            status=CheckStatus.UNSUPPORTED, detail="no record of an agreement"
        ),
    )


def exist_fail() -> Claim:
    return Claim(
        claim_id="x-1",
        type=ClaimType.C_EXIST,
        span="See INV-9999.",
        token="INV-9999",
        check_result=CheckResult(
            status=CheckStatus.FAIL, detail="no such document in the ledger"
        ),
    )


def all_pass() -> Claim:
    return Claim(
        claim_id="p-1",
        type=ClaimType.C_SUM,
        span="You owe $1,500.00.",
        token="$1,500.00",
        check_result=CheckResult(
            status=CheckStatus.PASS,
            expected="$1,500.00",
            actual="$1,500.00",
            cited_records=["NET"],
        ),
    )


def test_repeated_hash_converts_revise_to_escalate_revision_loop():
    ledger = make_ledger()
    decision = decide([sum_fail()], ledger, 1, ["h1", "h2", "h1"])
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "revision loop"
    assert decision.payload == "h1", "the repeated hash is the evidence"
    assert decision.findings, "the underlying findings travel with the escalation"


def test_distinct_hashes_leave_revise_alone():
    ledger = make_ledger()
    decision = decide([sum_fail()], ledger, 1, ["h1", "h2"])
    assert decision.action is GateAction.REVISE


def test_guard_applies_to_the_fuzzy_revise_path_too():
    ledger = make_ledger()
    decision = decide([fuzzy_unsupported()], ledger, 0, ["h1", "h1"])
    assert decision.action is GateAction.ESCALATE
    assert decision.reason == "revision loop"


def test_guard_does_not_touch_decisions_that_already_terminate():
    ledger = make_ledger()
    escalate = decide([exist_fail()], ledger, 0, ["h1", "h1"])
    assert escalate.action is GateAction.ESCALATE
    assert escalate.reason == "fabricated document reference", (
        "an existing escalation keeps its own reason"
    )
    human = decide([all_pass()], ledger, 0, ["h1", "h1"])
    assert human.action is GateAction.HUMAN_GATE


def test_empty_and_single_hash_history_never_trip_the_guard():
    ledger = make_ledger()
    assert decide([sum_fail()], ledger, 0, []).action is GateAction.REVISE
    assert decide([sum_fail()], ledger, 0, ["only"]).action is GateAction.REVISE


def test_revision_loop_escalation_maps_to_an_other_gap():
    ledger = make_ledger()
    decision = decide([sum_fail()], ledger, 1, ["h1", "h1"])
    gap = derive_gap(decision, ledger, "g-loop")
    assert gap is not None
    assert gap.missing is GapCategory.OTHER
    assert gap.resolvable_by == "human"
    assert gap.scenario_id == "T-OSC"
