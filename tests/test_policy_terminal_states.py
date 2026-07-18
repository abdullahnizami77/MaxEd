"""Invariant I5 posture: across a full sweep of synthetic finding
combinations, decide() only ever returns one of the four GateActions, REVISE
never occurs at revision_index >= 2, and a fuzzy-only revise never occurs
past index 0. Nothing the policy can express auto-sends.

Hermetic: hand-built ledgers and claims, no model, no network.
"""

from __future__ import annotations

import itertools
from datetime import date

from balancecheck.contracts.models import (
    Application,
    CheckResult,
    CheckStatus,
    Claim,
    ClaimType,
    ClientInfo,
    GateAction,
    GateDecision,
    Invoice,
    Ledger,
    Payment,
)
from balancecheck.gate.policy import decide

FLAGS = ("exist_fail", "unverifiable", "code_fail", "fuzzy_unsupported", "fuzzy_cannot")


def clean_ledger() -> Ledger:
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


def ambiguous_ledger() -> Ledger:
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
        payments=[Payment(id="PMT-0300", date=date(2026, 3, 20), amount_cents=240000)],
        credit_memos=[],
        applications=[],
    )


def _claim(
    cid: str, ctype: ClaimType, status: CheckStatus, span: str, **kw: str
) -> Claim:
    return Claim(
        claim_id=cid,
        type=ctype,
        span=span,
        token=kw.pop("token", ""),
        subject_id=kw.pop("subject", ""),
        check_result=CheckResult(status=status, **kw),
    )


def build_claims(flags: set[str]) -> list[Claim]:
    claims = [
        _claim(
            "p-1",
            ClaimType.C_EXIST,
            CheckStatus.PASS,
            "See INV-1012.",
            token="INV-1012",
        ),
        _claim(
            "p-2",
            ClaimType.C_SUM,
            CheckStatus.PASS,
            "You owe $1,500.00.",
            token="$1,500.00",
            expected="$1,500.00",
            actual="$1,500.00",
        ),
    ]
    if "exist_fail" in flags:
        claims.append(
            _claim(
                "x-1",
                ClaimType.C_EXIST,
                CheckStatus.FAIL,
                "See INV-9999.",
                token="INV-9999",
                detail="no such document in the ledger",
            )
        )
    if "unverifiable" in flags:
        claims.append(
            _claim(
                "u-1",
                ClaimType.C_AMT,
                CheckStatus.UNVERIFIABLE,
                "Remit half of the balance.",
                token="half of the balance",
            )
        )
    if "code_fail" in flags:
        claims.append(
            _claim(
                "s-1",
                ClaimType.C_SUM,
                CheckStatus.FAIL,
                "You owe $2,400.00.",
                token="$2,400.00",
                expected="$1,500.00",
                actual="$2,400.00",
            )
        )
    if "fuzzy_unsupported" in flags:
        claims.append(
            _claim(
                "z-1",
                ClaimType.C_FUZZY,
                CheckStatus.UNSUPPORTED,
                "We agreed to waive the late fee.",
            )
        )
    if "fuzzy_cannot" in flags:
        claims.append(
            _claim(
                "q-1",
                ClaimType.C_FUZZY,
                CheckStatus.CANNOT_DETERMINE,
                "Per our call, terms were extended.",
            )
        )
    return claims


def test_sweep_only_the_four_gate_actions_and_bounded_revision():
    ledgers = (clean_ledger(), ambiguous_ledger())
    actions = set(GateAction)
    for bits in itertools.product((False, True), repeat=len(FLAGS)):
        flags = {name for name, bit in zip(FLAGS, bits) if bit}
        for ledger in ledgers:
            for revision_index in range(4):
                hashes = [f"distinct-{i}" for i in range(revision_index + 1)]
                decision = decide(
                    build_claims(flags), ledger, revision_index, hashes
                )
                assert isinstance(decision, GateDecision)
                assert decision.action in actions, (flags, revision_index)
                if revision_index >= 2:
                    assert decision.action is not GateAction.REVISE, (
                        flags,
                        revision_index,
                        "invariant I5: the revision budget is a hard bound",
                    )
                if decision.action is GateAction.REVISE:
                    assert revision_index < 2
                    if "code_fail" not in flags:
                        # Only the fuzzy row can have produced this revise,
                        # and the fuzzy budget is exactly one.
                        assert revision_index < 1, (flags, revision_index)


def test_empty_findings_terminate_deterministically():
    for revision_index in range(4):
        clean = decide(build_claims(set()), clean_ledger(), revision_index, ["h"])
        assert clean.action is GateAction.HUMAN_GATE
        ambiguous = decide(
            build_claims(set()), ambiguous_ledger(), revision_index, ["h"]
        )
        assert ambiguous.action is GateAction.ABSTAIN


def test_no_claims_at_all_still_terminates():
    decision = decide([], clean_ledger(), 0, [])
    assert decision.action is GateAction.HUMAN_GATE
    assert decision.reason == "all checks passed"
