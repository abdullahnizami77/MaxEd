"""Correct-by-construction reference drafts, rendered from ledger truth.

Three consumers, one source:
- stub mode: the canned drafter responses that let the whole system run end
  to end with no model and zero credentials,
- the error foundry: Tier 1 corruptions need a known-good draft to corrupt,
- the checks/oracle cross-battery: a draft that must verify clean on both
  paths, so a disagreement between them is a bug in one of them.

No model is involved. Every number is rendered by code from derive.py, so a
golden draft is correct by construction, not by luck.
"""

from __future__ import annotations

from balancecheck.contracts.models import Ledger
from balancecheck.substrate import derive
from balancecheck.substrate.money import render


def golden_draft(ledger: Ledger) -> str:
    """A correct client reply for this ledger, rendered deterministically."""
    c = ledger.client
    lines: list[str] = []
    lines.append(f"Dear {c.name} team,")
    lines.append("")
    lines.append(
        "Thank you for reaching out about your account. Here is the current "
        f"position as of {c.as_of.isoformat()}."
    )
    lines.append("")
    net = derive.net_balance(ledger)
    lines.append(f"The balance due on your account is {render(net)}.")
    lines.append("")

    open_invoices = [i for i in ledger.invoices if derive.open_amount(ledger, i.id) > 0]
    if open_invoices:
        lines.append("This balance reflects the following open invoices:")
        for inv in open_invoices:
            open_amt = derive.open_amount(ledger, inv.id)
            status = derive.invoice_status(ledger, inv.id).value
            lines.append(
                f"- {inv.id} dated {inv.date.isoformat()}, due {inv.due.isoformat()}: "
                f"{render(open_amt)} {status}."
            )
        lines.append("")

    unapplied_payments = [
        p for p in ledger.payments if derive.unapplied_amount(ledger, p.id) > 0
    ]
    for p in unapplied_payments:
        lines.append(
            f"We have received your payment {p.id} of "
            f"{render(derive.unapplied_amount(ledger, p.id))}, which has not yet been "
            "applied to a specific invoice. It is reflected in the balance above."
        )
    open_credits = [
        cm for cm in ledger.credit_memos if derive.unapplied_amount(ledger, cm.id) > 0
    ]
    for cm in open_credits:
        lines.append(
            f"An open credit memo {cm.id} of "
            f"{render(derive.unapplied_amount(ledger, cm.id))} is also reflected in the "
            "balance above."
        )
    if unapplied_payments or open_credits:
        lines.append("")

    lines.append(
        "If you have questions about any of the items above, please reply to this "
        "message and we will be glad to walk through them with you."
    )
    lines.append("")
    lines.append("Kind regards,")
    lines.append("Accounts Receivable")
    return "\n".join(lines)
