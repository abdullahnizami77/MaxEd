"""The decision matrix: plain rules, no model, first match wins (plan section 8).

What this module guarantees:

- decide() only ever returns one of the four GateActions (REVISE, ABSTAIN,
  ESCALATE, HUMAN_GATE); nothing here can express "send", which is the
  policy half of invariant I5 (the runner holds the other half: REVISE is
  never terminal and HUMAN_GATE always lands in front of a person).
- Findings are matched in severity order: fabricated reference, then
  unverifiable quantitative claim, then correctable code failures, then
  fuzzy unsupported, then fuzzy cannot-determine, then ledger ambiguity,
  then all-pass. The first matching row decides.
- REVISE is budgeted: code-check corrections revise while revision_index is
  below REVISION_BUDGET (2); an unsupported fuzzy claim revises exactly once.
  At or past the budget the same findings escalate, so the loop always
  terminates.
- The oscillation guard converts any REVISE into ESCALATE "revision loop"
  when the current draft hash already appeared earlier: a model that cycles
  is escalated, not spun.
- A claim that somehow reaches the gate with no check_result is treated as
  unverifiable and escalated, never ignored (the I3/I11 posture: nothing is
  silently unchecked).
- precheck_abstain() exposes the ambiguity rule on the raw ledger so the
  runner can abstain before spending a single model call; decide() keeps the
  same rule as a backstop.
"""

from __future__ import annotations

from balancecheck.contracts.models import (
    CheckStatus,
    Claim,
    ClaimType,
    Finding,
    GateAction,
    GateDecision,
    Ledger,
)
from balancecheck.checks.checks import NET_LABEL
from balancecheck.substrate import derive
from balancecheck.substrate.money import render

REVISION_BUDGET = 2        # C-AMT/C-SUM/C-STATUS/C-DATE corrections: revise below this
FUZZY_REVISION_BUDGET = 1  # an unsupported fuzzy claim is revised exactly once

# Decision reasons are a closed vocabulary shared with gate/gaps.py, which
# keys its capability-gap categories off these exact strings.
REASON_FABRICATED = "fabricated document reference"
REASON_UNVERIFIABLE = "unverifiable quantitative claim"
REASON_CORRECTABLE = "correctable code-check failure"
REASON_BUDGET_EXHAUSTED = "revision budget exhausted"
REASON_UNSUPPORTED = "unsupported fuzzy claim"
REASON_UNSUPPORTED_EXHAUSTED = "unsupported fuzzy claim after revision"
REASON_CANNOT_DETERMINE = "fuzzy claim cannot be determined from the records"
REASON_AMBIGUOUS = "ambiguous allocation"
REASON_ALL_PASS = "all checks passed"
REASON_LOOP = "revision loop"
REASON_MISSING_NET = "draft never states the net balance due"

WOULD_RESOLVE_CANNOT_DETERMINE = "a record or human confirmation of the claimed arrangement"

_CODE_FAIL_KINDS: dict[ClaimType, str] = {
    ClaimType.C_AMT: "c_amt_fail",
    ClaimType.C_SUM: "c_sum_fail",
    ClaimType.C_STATUS: "c_status_fail",
    ClaimType.C_DATE: "c_date_fail",
}

_CONTEXT_BY_TYPE: dict[ClaimType, str] = {
    ClaimType.C_SUM: "the total balance due",
    ClaimType.C_DATE: "the statement",
    ClaimType.C_AMT: "this account",
    ClaimType.C_STATUS: "this document",
}


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def _correction_line(claim: Claim) -> str:
    """One correction sentence for one failed claim."""
    result = claim.check_result
    if result is not None and result.expected:
        subject = claim.subject_id or _CONTEXT_BY_TYPE.get(claim.type, "this account")
        actual = result.actual or claim.token or claim.span
        return f"The ledger shows {result.expected} for {subject}; your draft says {actual}."
    return (
        "The records do not support this statement; remove it or reword it"
        f" to match the records: {claim.span}"
    )


def build_correction(failed_claims: list[Claim]) -> str:
    """The correction block injected into a revision prompt, one line per
    failed claim, each carrying the expected and actual renderings."""
    return "\n".join(_correction_line(c) for c in failed_claims)


# ---------------------------------------------------------------------------
# Ambiguity (shared by precheck_abstain and the in-matrix backstop)
# ---------------------------------------------------------------------------


def _ambiguity_decision(ledger: Ledger) -> GateDecision | None:
    ambiguous = derive.ambiguous_allocations(ledger)
    if not ambiguous:
        return None
    findings = [
        Finding(
            kind="ambiguous_allocation",
            detail=f"{payment_id} could apply to any of: {', '.join(invoice_ids)}",
        )
        for payment_id, invoice_ids in ambiguous
    ]
    payload = "; ".join(f.detail for f in findings)
    return GateDecision(
        action=GateAction.ABSTAIN,
        reason=REASON_AMBIGUOUS,
        findings=findings,
        payload=payload,
    )


def precheck_abstain(ledger: Ledger) -> GateDecision | None:
    """Abstain before drafting when the records cannot support any correct
    draft; None when drafting may proceed."""
    return _ambiguity_decision(ledger)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def _status_of(claim: Claim) -> CheckStatus | None:
    return claim.check_result.status if claim.check_result is not None else None


def _match_matrix(
    claims: list[Claim], ledger: Ledger, revision_index: int
) -> GateDecision:
    exist_fails = [
        c
        for c in claims
        if c.type is ClaimType.C_EXIST and _status_of(c) is CheckStatus.FAIL
    ]
    unverifiable = [
        c for c in claims if _status_of(c) is CheckStatus.UNVERIFIABLE or _status_of(c) is None
    ]
    code_fails = [
        c
        for c in claims
        if c.type in _CODE_FAIL_KINDS and _status_of(c) is CheckStatus.FAIL
    ]
    fuzzy_unsupported = [
        c
        for c in claims
        if c.type is ClaimType.C_FUZZY and _status_of(c) is CheckStatus.UNSUPPORTED
    ]
    fuzzy_cannot = [
        c
        for c in claims
        if c.type is ClaimType.C_FUZZY and _status_of(c) is CheckStatus.CANNOT_DETERMINE
    ]

    # Row 1: a fabricated document reference escalates immediately.
    if exist_fails:
        findings = [
            Finding(
                kind="c_exist_fail",
                claim_id=c.claim_id,
                span=c.span,
                detail=c.check_result.detail if c.check_result else "",
            )
            for c in exist_fails
        ]
        payload = "; ".join(f"{c.span} [{c.token}]" for c in exist_fails)
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_FABRICATED,
            findings=findings,
            payload=payload,
        )

    # Row 2: a quantitative span the code could not pin is escalated, never
    # dropped and never model-routed (invariant I11 downstream).
    if unverifiable:
        findings = [
            Finding(
                kind="unverifiable_quant",
                claim_id=c.claim_id,
                span=c.span,
                detail=(
                    c.check_result.detail
                    if c.check_result is not None
                    else "claim was never checked"
                ),
            )
            for c in unverifiable
        ]
        payload = "; ".join(c.span for c in unverifiable)
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_UNVERIFIABLE,
            findings=findings,
            payload=payload,
        )

    # Row 3: computable failures are corrected while budget remains.
    if code_fails:
        findings = [
            Finding(
                kind=_CODE_FAIL_KINDS[c.type],
                claim_id=c.claim_id,
                span=c.span,
                detail=c.check_result.detail if c.check_result else "",
                correction=_correction_line(c),
            )
            for c in code_fails
        ]
        if revision_index < REVISION_BUDGET:
            return GateDecision(
                action=GateAction.REVISE,
                reason=REASON_CORRECTABLE,
                findings=findings,
                payload=build_correction(code_fails),
            )
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_BUDGET_EXHAUSTED,
            findings=findings,
            payload=build_correction(code_fails),
        )

    # Row 4: an unsupported fuzzy claim is revised exactly once.
    if fuzzy_unsupported:
        findings = [
            Finding(
                kind="fuzzy_unsupported",
                claim_id=c.claim_id,
                span=c.span,
                detail=c.check_result.detail if c.check_result else "",
                correction=_correction_line(c),
            )
            for c in fuzzy_unsupported
        ]
        payload = "; ".join(c.span for c in fuzzy_unsupported)
        if revision_index < FUZZY_REVISION_BUDGET:
            return GateDecision(
                action=GateAction.REVISE,
                reason=REASON_UNSUPPORTED,
                findings=findings,
                payload=payload,
            )
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_UNSUPPORTED_EXHAUSTED,
            findings=findings,
            payload=payload,
        )

    # Row 5: the records are silent and a human may not be; escalate with
    # what would resolve it.
    if fuzzy_cannot:
        findings = [
            Finding(
                kind="fuzzy_cannot_determine",
                claim_id=c.claim_id,
                span=c.span,
                detail=c.check_result.detail if c.check_result else "",
            )
            for c in fuzzy_cannot
        ]
        payload = "; ".join(
            f"{c.span} | would resolve: {WOULD_RESOLVE_CANNOT_DETERMINE}"
            for c in fuzzy_cannot
        )
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_CANNOT_DETERMINE,
            findings=findings,
            payload=payload,
        )

    # Row 6: ledger-level ambiguity (backstop; the runner prechecks this).
    ambiguity = _ambiguity_decision(ledger)
    if ambiguity is not None:
        return ambiguity

    # Row 7 (adversarial-review hardening): every individual claim can be
    # true while the draft misleads by omission. A balance-due reply that
    # never states the net balance is incomplete by construction, so the
    # gate requires one passing claim citing the computed net. Claim-level
    # checks cannot see a missing claim; this row can.
    net = derive.net_balance(ledger)
    if net != 0 and not any(
        c.check_result is not None
        and c.check_result.status is CheckStatus.PASS
        and NET_LABEL in c.check_result.cited_records
        for c in claims
    ):
        finding = Finding(
            kind="missing_net_statement",
            detail="no sentence states the computed net balance due",
            correction=(
                f"The draft never states the balance due. The ledger computes "
                f"{render(net)}; state it exactly once as the balance due."
            ),
        )
        if revision_index < REVISION_BUDGET:
            return GateDecision(
                action=GateAction.REVISE,
                reason=REASON_MISSING_NET,
                findings=[finding],
                payload=finding.correction,
            )
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_MISSING_NET,
            findings=[finding],
            payload=finding.detail,
        )

    # Row 8: everything passed; a human still approves (invariant I5).
    return GateDecision(
        action=GateAction.HUMAN_GATE, reason=REASON_ALL_PASS, findings=[]
    )


def _repeats_earlier_hash(draft_hashes: list[str]) -> bool:
    return bool(draft_hashes) and draft_hashes[-1] in draft_hashes[:-1]


def decide(
    claims: list[Claim],
    ledger: Ledger,
    revision_index: int,
    draft_hashes: list[str],
) -> GateDecision:
    """One gate decision for one verified draft; first matching row wins.

    draft_hashes carries the sha256 of every draft so far, current last;
    a current hash that already appeared converts any REVISE into
    ESCALATE "revision loop" (the oscillation guard).
    """
    decision = _match_matrix(claims, ledger, revision_index)
    if decision.action is GateAction.REVISE and _repeats_earlier_hash(draft_hashes):
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_LOOP,
            findings=decision.findings,
            payload=draft_hashes[-1],
        )
    return decision
