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
    AmountRole,
    CheckStatus,
    Claim,
    ClaimType,
    Finding,
    GateAction,
    GateDecision,
    Ledger,
)
from balancecheck.checks.checks import NET_LABEL, parse_claim_amount
from balancecheck.substrate import derive
from balancecheck.substrate.money import cents, render

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
REASON_INCOMPLETE = "draft is missing required content"
REASON_ITEMIZATION = "itemized amounts contradict the computed totals"

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
, draft: str = "") -> GateDecision:
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

    # Rows 7 and 8 (adversarial-review hardening): every individual claim
    # can be true while the draft misleads as a whole. Claim-level checks
    # cannot see a missing claim or an inconsistent presentation; these
    # rows can, and both were added after live drafts demonstrated exactly
    # these escapes.

    # Row 7: itemization consistency. When the draft itemizes every open
    # invoice and the ledger has no unapplied sources, the itemized amounts
    # must sum to the open-invoice total; listing full original amounts as
    # what is owed alongside a smaller correct net is a wrong presentation
    # made of individually true numbers.
    inconsistency = _itemization_inconsistency(claims, ledger)
    if inconsistency is not None:
        if revision_index < REVISION_BUDGET:
            return GateDecision(
                action=GateAction.REVISE,
                reason=REASON_ITEMIZATION,
                findings=[inconsistency],
                payload=inconsistency.correction,
            )
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_ITEMIZATION,
            findings=[inconsistency],
            payload=inconsistency.detail,
        )

    # Row 8: completeness. A balance-due reply must state the net balance,
    # itemize the open invoices, and mention unapplied cash and open
    # credits when the ledger carries them. With the draft text in hand the
    # full applicability-gated checklist gates; without it (bare claim-set
    # callers) the net-statement citation is the fallback signal.
    missing = _missing_completeness(claims, ledger, draft)
    if missing:
        findings = [
            Finding(
                kind=f"missing_{name}",
                detail=f"required content absent: {name}",
                correction=hint,
            )
            for name, hint in missing
        ]
        correction = " ".join(f.correction for f in findings)
        if revision_index < REVISION_BUDGET:
            return GateDecision(
                action=GateAction.REVISE,
                reason=REASON_INCOMPLETE,
                findings=findings,
                payload=correction,
            )
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_INCOMPLETE,
            findings=findings,
            payload="; ".join(f.detail for f in findings),
        )

    # Row 9: everything passed; a human still approves (invariant I5).
    return GateDecision(
        action=GateAction.HUMAN_GATE, reason=REASON_ALL_PASS, findings=[]
    )


def _repeats_earlier_hash(draft_hashes: list[str]) -> bool:
    return bool(draft_hashes) and draft_hashes[-1] in draft_hashes[:-1]


def _itemization_inconsistency(claims: list[Claim], ledger: Ledger) -> Finding | None:
    open_invoices = {
        i.id: derive.open_amount(ledger, i.id)
        for i in ledger.invoices
        if derive.open_amount(ledger, i.id) > 0
    }
    if len(open_invoices) < 2:
        return None
    # Only when nothing is unapplied: then the open-invoice total IS the
    # net, and an itemization summing to anything else contradicts it.
    if derive.unapplied_cash_total(ledger) != 0 or derive.unapplied_credit_total(ledger) != 0:
        return None
    # The figure the draft presents as each invoice's owed amount: a role=open
    # amount when the draft labels it open, otherwise a bare (role=unknown)
    # amount stated against the invoice. Amounts the draft explicitly labels
    # original or applied are not the owed figure and are ignored, so a
    # transparent decomposition ("originally X, of which Y paid, leaving Z
    # open") is not misread as an inconsistent itemization.
    open_figs: dict[str, int] = {}
    unknown_figs: dict[str, int] = {}
    for c in claims:
        if c.type is not ClaimType.C_AMT or c.check_result is None:
            continue
        if c.check_result.status is not CheckStatus.PASS:
            continue
        bound = set(c.subject_ids) or ({c.subject_id} if c.subject_id else set())
        if len(bound) != 1:
            continue
        sid = next(iter(bound))
        if sid not in open_invoices:
            continue
        try:
            value = int(parse_claim_amount(c.token))
        except ValueError:
            continue
        if c.role == AmountRole.OPEN:
            open_figs[sid] = value
        elif c.role == AmountRole.UNKNOWN:
            unknown_figs.setdefault(sid, value)
    itemized = {sid: open_figs.get(sid, unknown_figs.get(sid)) for sid in open_invoices}
    itemized = {sid: v for sid, v in itemized.items() if v is not None}
    if set(itemized) != set(open_invoices):
        return None  # not a full itemization; nothing to cross-check
    itemized_sum = sum(itemized.values())
    open_total = int(derive.open_invoice_total(ledger))
    if itemized_sum == open_total:
        return None
    return Finding(
        kind="itemization_inconsistent",
        detail=(
            f"itemized invoice amounts sum to {render(cents(itemized_sum))} but the "
            f"open-invoice total is {render(cents(open_total))}"
        ),
        correction=(
            "The itemized amounts contradict the computed balance: list each open "
            "invoice at its OPEN amount ("
            + ", ".join(f"{k} {render(v)}" for k, v in sorted(open_invoices.items()))
            + ")."
        ),
    )


def _missing_completeness(
    claims: list[Claim], ledger: Ledger, draft: str
) -> list[tuple[str, str]]:
    if draft:
        from balancecheck.bench.score import completeness_items

        return [
            (item["name"], item["hint"])
            for item in completeness_items(draft, ledger)
            if item["applicable"] and not item["present"]
        ]
    # Fallback for bare claim-set callers: the net-statement citation.
    net = derive.net_balance(ledger)
    if net != 0 and not any(
        c.check_result is not None
        and c.check_result.status is CheckStatus.PASS
        and NET_LABEL in c.check_result.cited_records
        for c in claims
    ):
        return [
            (
                "net_balance_stated",
                f"The draft never states the balance due. The ledger computes "
                f"{render(net)}; state it exactly once as the balance due.",
            )
        ]
    return []


def decide(
    claims: list[Claim],
    ledger: Ledger,
    revision_index: int,
    draft_hashes: list[str],
    draft: str = "",
) -> GateDecision:
    """One gate decision for one verified draft; first matching row wins.

    draft_hashes carries the sha256 of every draft so far, current last;
    a current hash that already appeared converts any REVISE into
    ESCALATE "revision loop" (the oscillation guard).
    """
    decision = _match_matrix(claims, ledger, revision_index, draft)
    if decision.action is GateAction.REVISE and _repeats_earlier_hash(draft_hashes):
        return GateDecision(
            action=GateAction.ESCALATE,
            reason=REASON_LOOP,
            findings=decision.findings,
            payload=draft_hashes[-1],
        )
    return decision
