"""Capability gaps: every abstain and escalate names what was missing (plan section 10).

What this module guarantees:

- derive_gap() maps every terminal ABSTAIN or ESCALATE decision to exactly
  one CapabilityGapEvent with a closed GapCategory, keyed off the policy's
  closed reason vocabulary, so gaps aggregate instead of free-texting.
  HUMAN_GATE and REVISE yield None: nothing was missing.
- An escalate reason outside the mapped vocabulary still produces a gap
  (OTHER, resolvable by a human); no terminal failure is ever gap-less.
- aggregate_gaps() folds gap events into {category value: [gen_ids]}, the
  shape the report renders, which is what turns six allocation escalations
  into a roadmap item instead of a log curiosity.
"""

from __future__ import annotations

from balancecheck.contracts.models import (
    CapabilityGapEvent,
    GapCategory,
    GateAction,
    GateDecision,
    Ledger,
)
from balancecheck.gate import policy


def derive_gap(
    decision: GateDecision, ledger: Ledger, gen_id: str
) -> CapabilityGapEvent | None:
    """The capability gap behind one terminal decision, or None for the
    terminal states (HUMAN_GATE, REVISE-in-flight) where nothing was missing."""
    if decision.action in (GateAction.HUMAN_GATE, GateAction.REVISE):
        return None
    detail = decision.payload or decision.reason

    if decision.action is GateAction.ABSTAIN:
        return CapabilityGapEvent(
            gen_id=gen_id,
            scenario_id=ledger.scenario_id,
            missing=GapCategory.ALLOCATION_REFERENCE,
            detail=detail,
            would_need="remittance advice tying the payment to an invoice",
            resolvable_by="human",
        )

    # ESCALATE: category follows the policy's closed reason vocabulary.
    if decision.reason == policy.REASON_UNVERIFIABLE:
        return CapabilityGapEvent(
            gen_id=gen_id,
            scenario_id=ledger.scenario_id,
            missing=GapCategory.UNVERIFIABLE_QUANT,
            detail=detail,
            would_need="a claim normalizer for non-canonical amount phrasing",
            resolvable_by="tool",
        )
    if decision.reason == policy.REASON_CANNOT_DETERMINE:
        return CapabilityGapEvent(
            gen_id=gen_id,
            scenario_id=ledger.scenario_id,
            missing=GapCategory.AMBIGUOUS_REFERENCE,
            detail=detail,
            would_need=policy.WOULD_RESOLVE_CANNOT_DETERMINE,
            resolvable_by="human",
        )
    if decision.reason == policy.REASON_FABRICATED:
        return CapabilityGapEvent(
            gen_id=gen_id,
            scenario_id=ledger.scenario_id,
            missing=GapCategory.DOCUMENT_ABSENT,
            detail=detail,
            would_need="drafter constrained to the document set",
            resolvable_by="tool",
        )
    # Revision budget exhausted, revision loop, unsupported-after-revision,
    # and anything the vocabulary does not name: a human takes over.
    return CapabilityGapEvent(
        gen_id=gen_id,
        scenario_id=ledger.scenario_id,
        missing=GapCategory.OTHER,
        detail=detail,
        would_need="human review of a draft the loop could not converge",
        resolvable_by="human",
    )


def aggregate_gaps(events: list[CapabilityGapEvent]) -> dict[str, list[str]]:
    """Fold gap events into {category value: [gen_ids]} for the report."""
    out: dict[str, list[str]] = {}
    for event in events:
        out.setdefault(event.missing.value, []).append(event.gen_id)
    return out
