"""Derived ledger truth: every number a draft may claim, computed by code.

Semantics, stated once:
- open_amount(invoice) = amount - sum(applications targeting it)
- unapplied_amount(source) = amount - sum(applications from it), for payments
  and credit memos alike
- open_invoice_total = sum of open invoice amounts
- net_balance = open_invoice_total - unapplied cash - unapplied credits.
  This is the honest number to tell a client: money already received or
  credited but not yet applied still reduces what they owe. A draft that
  states the sum of unpaid invoices while ignoring received-but-unapplied
  cash is exactly the Tier 2 failure the error foundry produces.

The signature is DETECTED from the records, not read from the foundry's
intent labels; a fixture test asserts the two agree, which keeps the foundry
honest about the structures it claims to have built.

recompute_net_balance_naive is a deliberately independent implementation
(event-replay over a per-document dict, no shared helpers) backing invariant
I2, so the fixture-balance test is not tautological.
"""

from __future__ import annotations

from datetime import date, timedelta

from balancecheck.contracts.models import (
    Application,
    Invoice,
    InvoiceStatus,
    Ledger,
    STRUCTURE_LABELS,
)
from balancecheck.substrate.money import Cents, cents

# A document dated within this many days before as_of carries cutoff risk:
# a crossing payment may not yet be reflected in the books.
CUTOFF_WINDOW_DAYS = 5


# ---------------------------------------------------------------------------
# Per-document derivations
# ---------------------------------------------------------------------------


def applied_to_invoice(ledger: Ledger, invoice_id: str) -> Cents:
    return cents(sum(a.amount_cents for a in ledger.applications if a.target_invoice == invoice_id))


def open_amount(ledger: Ledger, invoice_id: str) -> Cents:
    inv = _invoice(ledger, invoice_id)
    return cents(inv.amount_cents - applied_to_invoice(ledger, invoice_id))


def applied_from_source(ledger: Ledger, source_id: str) -> Cents:
    return cents(sum(a.amount_cents for a in ledger.applications if a.source_id == source_id))


def unapplied_amount(ledger: Ledger, source_id: str) -> Cents:
    doc = _source(ledger, source_id)
    return cents(doc.amount_cents - applied_from_source(ledger, source_id))


def invoice_status(ledger: Ledger, invoice_id: str) -> InvoiceStatus:
    inv = _invoice(ledger, invoice_id)
    if open_amount(ledger, invoice_id) == 0:
        return InvoiceStatus.PAID
    if inv.due < ledger.client.as_of:
        return InvoiceStatus.OVERDUE
    return InvoiceStatus.OUTSTANDING



# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def open_invoice_total(ledger: Ledger) -> Cents:
    return cents(sum(open_amount(ledger, i.id) for i in ledger.invoices))


def unapplied_cash_total(ledger: Ledger) -> Cents:
    return cents(sum(unapplied_amount(ledger, p.id) for p in ledger.payments))


def unapplied_credit_total(ledger: Ledger) -> Cents:
    return cents(sum(unapplied_amount(ledger, c.id) for c in ledger.credit_memos))


def net_balance(ledger: Ledger) -> Cents:
    return cents(
        open_invoice_total(ledger) - unapplied_cash_total(ledger) - unapplied_credit_total(ledger)
    )


# ---------------------------------------------------------------------------
# Structure detection (the signature) and ambiguity
# ---------------------------------------------------------------------------


def ambiguous_allocations(ledger: Ledger) -> list[tuple[str, list[str]]]:
    """Unapplied payments or credit memos whose amount equals the open amount
    of two or more invoices: the records genuinely do not say which invoice
    the money was meant for, so no correct confident draft exists (the
    abstain trigger).
    """
    out: list[tuple[str, list[str]]] = []
    sources: list = list(ledger.payments) + list(ledger.credit_memos)
    for src_doc in sources:
        un = unapplied_amount(ledger, src_doc.id)
        if un <= 0:
            continue
        matches = [i.id for i in ledger.invoices if open_amount(ledger, i.id) == un and un > 0]
        if len(matches) >= 2:
            out.append((src_doc.id, matches))
    return out


def signature(ledger: Ledger) -> dict[str, bool]:
    """Detected from the records, never from the foundry's labels."""
    near_dup = False
    amounts = [i.amount_cents for i in ledger.invoices]
    near_dup = len(amounts) != len(set(amounts))

    cutoff_edge = ledger.client.as_of - timedelta(days=CUTOFF_WINDOW_DAYS)
    cutoff = (
        any(p.date >= cutoff_edge for p in ledger.payments)
        or any(i.date >= cutoff_edge for i in ledger.invoices)
        or any(c.date >= cutoff_edge for c in ledger.credit_memos)
    )

    sig = {
        "has_unapplied_cash": any(unapplied_amount(ledger, p.id) > 0 for p in ledger.payments),
        "has_partial": any(
            0 < applied_to_invoice(ledger, i.id) < i.amount_cents for i in ledger.invoices
        ),
        "has_open_credit": any(unapplied_amount(ledger, c.id) > 0 for c in ledger.credit_memos),
        "has_near_duplicate": near_dup,
        "has_cutoff_risk": cutoff,
        "has_ambiguous_allocation": bool(ambiguous_allocations(ledger)),
    }
    assert set(sig) == set(STRUCTURE_LABELS)
    return sig



# ---------------------------------------------------------------------------
# Structural validation (fixture sanity, not draft verification)
# ---------------------------------------------------------------------------


def validate_ledger(ledger: Ledger) -> list[str]:
    problems: list[str] = []
    invoice_ids = {i.id for i in ledger.invoices}
    source_ids = {p.id for p in ledger.payments} | {c.id for c in ledger.credit_memos}
    all_ids = invoice_ids | source_ids
    if len(all_ids) != len(ledger.invoices) + len(ledger.payments) + len(ledger.credit_memos):
        problems.append("duplicate document IDs")
    for a in ledger.applications:
        if a.source_id not in source_ids:
            problems.append(f"application from unknown source {a.source_id}")
        if a.target_invoice not in invoice_ids:
            problems.append(f"application to unknown invoice {a.target_invoice}")
    for i in ledger.invoices:
        if applied_to_invoice(ledger, i.id) > i.amount_cents:
            problems.append(f"invoice {i.id} over-applied")
        if i.due < i.date:
            problems.append(f"invoice {i.id} due before issue date")
        if i.date > ledger.client.as_of:
            problems.append(f"invoice {i.id} dated after as_of")
    for p in ledger.payments:
        if applied_from_source(ledger, p.id) > p.amount_cents:
            problems.append(f"payment {p.id} over-applied")
        if p.date > ledger.client.as_of:
            problems.append(f"payment {p.id} dated after as_of")
    for c in ledger.credit_memos:
        if applied_from_source(ledger, c.id) > c.amount_cents:
            problems.append(f"credit memo {c.id} over-applied")
        if c.date > ledger.client.as_of:
            problems.append(f"credit memo {c.id} dated after as_of")
    return problems


# ---------------------------------------------------------------------------
# The independent recompute (invariant I2)
# ---------------------------------------------------------------------------


def recompute_net_balance_naive(ledger_json: dict) -> int:
    """Recompute net balance from raw JSON by event replay.

    Deliberately shares no code with the functions above: raw dicts in, a
    running integer out. If derive.py has a bug, this disagrees and the
    fixture test fails; that is the point.
    """
    remaining: dict[str, int] = {}
    for inv in ledger_json["invoices"]:
        remaining[inv["id"]] = remaining.get(inv["id"], 0) + inv["amount_cents"]
    credit_pool: dict[str, int] = {}
    for pay in ledger_json["payments"]:
        credit_pool[pay["id"]] = credit_pool.get(pay["id"], 0) + pay["amount_cents"]
    for cm in ledger_json["credit_memos"]:
        credit_pool[cm["id"]] = credit_pool.get(cm["id"], 0) + cm["amount_cents"]
    for app in ledger_json["applications"]:
        remaining[app["target_invoice"]] -= app["amount_cents"]
        credit_pool[app["source_id"]] -= app["amount_cents"]
    total_open = 0
    for v in remaining.values():
        total_open += v
    leftover = 0
    for v in credit_pool.values():
        leftover += v
    return total_open - leftover


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoice(ledger: Ledger, invoice_id: str) -> Invoice:
    for i in ledger.invoices:
        if i.id == invoice_id:
            return i
    raise KeyError(f"no invoice {invoice_id} in {ledger.scenario_id}")


def _source(ledger: Ledger, source_id: str):
    for p in ledger.payments:
        if p.id == source_id:
            return p
    for c in ledger.credit_memos:
        if c.id == source_id:
            return c
    raise KeyError(f"no payment or credit memo {source_id} in {ledger.scenario_id}")


def document_ids(ledger: Ledger) -> set[str]:
    return (
        {i.id for i in ledger.invoices}
        | {p.id for p in ledger.payments}
        | {c.id for c in ledger.credit_memos}
    )
