"""The code-grounded scorer: the primary before/after instrument.

What this module guarantees:

- grounding() counts only code-checkable claim types (CODE_CHECKED_TYPES) and
  is strict about the numerator: a claim counts as grounded only when its
  check_result status is PASS. FAIL, UNVERIFIABLE, and a claim that never
  received a check_result all sit in the denominator and not the numerator,
  because an unresolvable number is an unverified number.
- completeness() is an applicability-gated checklist computed entirely by
  code against derive.py truth: items that do not apply to a ledger (no open
  invoices, no unapplied cash, no open credit) are excluded from the
  denominator, so simpler ledgers are not penalized.
- build_score_event() emits the ScoreEvent record the before/after report
  reads. The stage field is pinned by the caller (first_pass or final)
  because the whole before/after result depends on measuring the right draft
  stage.

This module exists from the moment checks/ does (plan v3, section 0 row F)
so the loop can be measured early. All money here is integer cents rendered
through money.render; no float literal appears in this module.
"""

from __future__ import annotations

import re

from typing import Literal

from balancecheck.contracts.models import (
    CODE_CHECKED_TYPES,
    CheckStatus,
    Claim,
    Ledger,
    ScoreEvent,
)
from balancecheck.substrate import derive
from balancecheck.substrate.money import render


def grounding(claims: list[Claim]) -> tuple[int, int]:
    """(code-checkable claims whose status is PASS, code-checkable claims).

    UNVERIFIABLE and FAIL both count in the denominator and not the
    numerator; so does a code-checkable claim with no check_result at all.
    Model-checked claims (C-FUZZY) appear in neither count: this is the code
    layer's number, by design.
    """
    passed = 0
    total = 0
    for claim in claims:
        if claim.type not in CODE_CHECKED_TYPES:
            continue
        total += 1
        if claim.check_result is not None and claim.check_result.status is CheckStatus.PASS:
            passed += 1
    return passed, total


def completeness(draft: str, ledger: Ledger) -> tuple[int, int]:
    """(applicable checklist items present in the draft, applicable items).

    The checklist, each item detected by plain string containment against
    code-rendered truth:

    1. Net balance stated: money.render(derive.net_balance(ledger)) appears.
       Always applicable.
    2. Open invoices itemized: every invoice with open_amount > 0 has its ID
       in the draft. Applicable only when at least one open invoice exists.
    3. Unapplied cash mentioned: applicable only when some payment has
       unapplied_amount > 0; present when every such payment appears by ID
       or by its rendered unapplied amount.
    4. Open credit mentioned: applicable only when some credit memo has
       unapplied_amount > 0; present when every such credit memo appears by
       ID or by its rendered unapplied amount.

    Non-applicable items are excluded from the denominator: simpler ledgers
    must not be penalized.
    """
    items = completeness_items(draft, ledger)
    applicable = [i for i in items if i["applicable"]]
    return sum(1 for i in applicable if i["present"]), len(applicable)


def completeness_items(draft: str, ledger: Ledger) -> list[dict]:
    """Item-level completeness detail: [{name, applicable, present, hint}].

    Computed from VERIFIED claims, not raw text: the net balance counts as
    stated only when a passing total claim cites the net (not when the net
    amount happens to appear as a line item), and a document counts as
    itemized/mentioned only when a passing claim actually references it. An
    empty draft (an abstention) yields no applicable items, so it is not
    scored as an incomplete draft.
    """
    from balancecheck.checks.checks import NET_LABEL, run_code_checks
    from balancecheck.contracts.models import ClaimType
    from balancecheck.drafting.surface import extract_claims

    if not draft.strip():
        return []

    claims = extract_claims(draft, ledger)
    run_code_checks(claims, ledger)
    passing = [c for c in claims if c.check_result and c.check_result.status is CheckStatus.PASS]

    def bound(c) -> set[str]:
        return set(c.subject_ids) or ({c.subject_id} if c.subject_id else set())

    def references(doc_id: str) -> bool:
        return any(
            (c.type is ClaimType.C_EXIST and c.token == doc_id)
            or (c.type is ClaimType.C_AMT and doc_id in bound(c))
            for c in passing
        )

    net = derive.net_balance(ledger)
    net_present = any(
        c.type is ClaimType.C_SUM and NET_LABEL in (c.check_result.cited_records or [])
        for c in passing
    )
    if not net_present and net == 0:
        net_present = (
            re.search(
                r"no\s+balance|nothing\s+(?:further\s+)?(?:is\s+)?owed|fully\s+paid|paid\s+in\s+full",
                draft,
                re.IGNORECASE,
            )
            is not None
        )
    items: list[dict] = [
        {
            "name": "net_balance_stated",
            "applicable": True,
            "present": net_present,
            "hint": f"state the balance due, {render(net)}, as the account total exactly once",
        }
    ]

    open_invoices = [i for i in ledger.invoices if derive.open_amount(ledger, i.id) > 0]
    items.append(
        {
            "name": "open_invoices_itemized",
            "applicable": bool(open_invoices),
            "present": bool(open_invoices) and all(references(inv.id) for inv in open_invoices),
            "hint": "itemize every open invoice by its ID: "
            + ", ".join(i.id for i in open_invoices),
        }
    )

    unapplied_payments = [p for p in ledger.payments if derive.unapplied_amount(ledger, p.id) > 0]
    items.append(
        {
            "name": "unapplied_cash_mentioned",
            "applicable": bool(unapplied_payments),
            "present": bool(unapplied_payments) and all(references(p.id) for p in unapplied_payments),
            "hint": "mention the unapplied payment(s) by ID: "
            + ", ".join(
                f"{p.id} ({render(derive.unapplied_amount(ledger, p.id))})"
                for p in unapplied_payments
            ),
        }
    )

    open_credits = [c for c in ledger.credit_memos if derive.unapplied_amount(ledger, c.id) > 0]
    items.append(
        {
            "name": "open_credit_mentioned",
            "applicable": bool(open_credits),
            "present": bool(open_credits) and all(references(c.id) for c in open_credits),
            "hint": "mention the open credit memo(s) by ID: "
            + ", ".join(
                f"{c.id} ({render(derive.unapplied_amount(ledger, c.id))})"
                for c in open_credits
            ),
        }
    )
    return items


def build_score_event(
    gen_id: str,
    scenario_id: str,
    pass_label: str,
    stage: Literal["first_pass", "final"],
    claims: list[Claim],
    draft: str,
    ledger: Ledger,
    revision_count: int,
    terminal_action: str,
    run_id: str = "",
) -> ScoreEvent:
    """Assemble the ScoreEvent for one generation at one measurement stage.

    Fills grounding_checked/grounding_total from grounding(claims) and
    completeness_present/completeness_total from completeness(draft, ledger).
    The caller pins the stage; this function never guesses it.
    """
    grounding_checked, grounding_total = grounding(claims)
    completeness_present, completeness_total = completeness(draft, ledger)
    return ScoreEvent(
        run_id=run_id,
        gen_id=gen_id,
        scenario_id=scenario_id,
        pass_label=pass_label,
        stage=stage,
        grounding_checked=grounding_checked,
        grounding_total=grounding_total,
        completeness_present=completeness_present,
        completeness_total=completeness_total,
        revision_count=revision_count,
        terminal_action=terminal_action,
    )
