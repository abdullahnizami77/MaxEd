"""Read-only accounting tools for the agentic revision recovery.

What this module guarantees:

- Every tool is a pure, deterministic, read-only wrapper over a Ledger and
  substrate/derive.py. No tool touches the filesystem, the network, SQL,
  eval, or any free-form query surface; the same Ledger and arguments always
  produce the same result, and the Ledger is never mutated.
- All money is integer cents internally (invariant I1). Every money value in
  a tool result is rendered through money.render, with the raw integer
  alongside under a key suffixed _cents.
- execute_tool never raises for bad input: an unknown tool name, arguments
  that fail JSON-Schema validation, or an unknown document ID all return
  {"ok": False, "error": "<reason>"} instead. Success is always
  {"ok": True, "data": {...}}.
- ID arguments are canonicalized before lookup with the same deterministic
  ledger-assisted logic the claim extractor uses. Chosen approach: this
  module IMPORTS the private helpers _canonical_prefix_id and
  _canonical_loose_id from drafting/surface.py (acceptable within the
  package) rather than reimplementing them, so the extractor and the tools
  can never drift apart on what "invoice 101" means. A genuinely unknown ID
  keeps its naive canonical form and surfaces as a typed error.
- Nothing here exposes or accepts gate actions. The tools expose only
  ledger-derived accounting facts; the agent's draft is judged elsewhere.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import jsonschema

from balancecheck.contracts.models import CreditMemo, Ledger, Payment
from balancecheck.drafting.surface import _canonical_loose_id, _canonical_prefix_id
from balancecheck.substrate import derive
from balancecheck.substrate.money import cents, render

# Argument-shaped ID forms, anchored: the whole argument must be one ID
# reference. These mirror the extractor's tolerant shapes (INV-101, INV_101,
# inv 101, INV101, "invoice 101", "payment no. 208", "credit memo 31").
_EXPLICIT_ARG_RE = re.compile(r"(INV|PMT|CM)[-_ ]?(\d+)", re.IGNORECASE)
_LOOSE_ARG_RE = re.compile(
    r"(invoice|payment|credit\s+memo)\s+(?:no\.?\s*|number\s*)?#?\s*(\d+)",
    re.IGNORECASE,
)


class ToolError(ValueError):
    """A typed per-call failure (unknown ID, wrong document kind).

    Raised by tool functions and converted by execute_tool into the
    {"ok": False, "error": ...} result shape; never propagated to callers
    of execute_tool.
    """


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def canonicalize_doc_id(raw: str, ledger: Ledger) -> str:
    """Canonicalize a document ID argument the way the extractor would.

    Tolerates prefix forms (INV-101, INV_101, inv 101, INV101) and loose
    forms (invoice 101, payment no. 208, credit memo 31), including
    zero-padding differences resolved against the ledger when exactly one
    document matches. Anything that does not parse as an ID reference is
    returned stripped but otherwise unchanged, so a fabricated or free-form
    string still fails lookup with a typed error instead of crashing.
    """
    text = raw.strip()
    m = _EXPLICIT_ARG_RE.fullmatch(text)
    if m:
        return _canonical_prefix_id(m.group(1).upper(), m.group(2), ledger)
    m = _LOOSE_ARG_RE.fullmatch(text)
    if m:
        return _canonical_loose_id(m.group(1), m.group(2), ledger)
    return text


# ---------------------------------------------------------------------------
# Shared row builders (every money value: rendered plus _cents)
# ---------------------------------------------------------------------------


def _invoice_row(ledger: Ledger, invoice_id: str) -> dict[str, Any]:
    inv = next(i for i in ledger.invoices if i.id == invoice_id)
    applied = derive.applied_to_invoice(ledger, invoice_id)
    open_amt = derive.open_amount(ledger, invoice_id)
    return {
        "id": inv.id,
        "date": inv.date.isoformat(),
        "due": inv.due.isoformat(),
        "original": render(cents(inv.amount_cents)),
        "original_cents": inv.amount_cents,
        "applied": render(applied),
        "applied_cents": applied,
        "open": render(open_amt),
        "open_cents": open_amt,
        "status": derive.invoice_status(ledger, invoice_id).value,
        "memo": inv.memo,
    }


def _source_row(ledger: Ledger, doc: Payment | CreditMemo) -> dict[str, Any]:
    applied = derive.applied_from_source(ledger, doc.id)
    unapplied = derive.unapplied_amount(ledger, doc.id)
    row: dict[str, Any] = {
        "type": "payment" if isinstance(doc, Payment) else "credit_memo",
        "id": doc.id,
        "date": doc.date.isoformat(),
        "original": render(cents(doc.amount_cents)),
        "original_cents": doc.amount_cents,
        "applied": render(applied),
        "applied_cents": applied,
        "unapplied": render(unapplied),
        "unapplied_cents": unapplied,
    }
    if isinstance(doc, Payment):
        row["method"] = doc.method
        row["reference"] = doc.reference
    else:
        row["reason"] = doc.reason
    return row


# ---------------------------------------------------------------------------
# The six tools (each: callable(ledger, **args) -> dict)
# ---------------------------------------------------------------------------


def get_account_summary(ledger: Ledger) -> dict[str, Any]:
    """Account-level totals and the detected structure signature."""
    open_total = derive.open_invoice_total(ledger)
    cash_total = derive.unapplied_cash_total(ledger)
    credit_total = derive.unapplied_credit_total(ledger)
    net = derive.net_balance(ledger)
    return {
        "client_id": ledger.client.id,
        "client_name": ledger.client.name,
        "as_of": ledger.client.as_of.isoformat(),
        "open_invoice_total": render(open_total),
        "open_invoice_total_cents": open_total,
        "unapplied_cash_total": render(cash_total),
        "unapplied_cash_total_cents": cash_total,
        "unapplied_credit_total": render(credit_total),
        "unapplied_credit_total_cents": credit_total,
        "net_balance_due": render(net),
        "net_balance_due_cents": net,
        "signature": derive.signature(ledger),
    }


def list_open_invoices(ledger: Ledger) -> dict[str, Any]:
    """Every invoice with an open amount strictly greater than zero."""
    rows = [
        _invoice_row(ledger, inv.id)
        for inv in ledger.invoices
        if derive.open_amount(ledger, inv.id) > 0
    ]
    return {"open_invoices": rows}


def get_invoice(ledger: Ledger, *, invoice_id: str) -> dict[str, Any]:
    """One invoice by (canonicalized) ID, with every application onto it."""
    canon = canonicalize_doc_id(invoice_id, ledger)
    if not any(i.id == canon for i in ledger.invoices):
        raise ToolError(f"unknown invoice {canon}")
    row = _invoice_row(ledger, canon)
    row["applications"] = [
        {
            "source_id": a.source_id,
            "amount": render(cents(a.amount_cents)),
            "amount_cents": a.amount_cents,
        }
        for a in ledger.applications
        if a.target_invoice == canon
    ]
    return row


def list_unapplied_sources(ledger: Ledger) -> dict[str, Any]:
    """Every payment or credit memo with unapplied money remaining."""
    docs: list[Payment | CreditMemo] = [*ledger.payments, *ledger.credit_memos]
    rows = [
        _source_row(ledger, doc)
        for doc in docs
        if derive.unapplied_amount(ledger, doc.id) > 0
    ]
    return {"unapplied_sources": rows}


def get_source(ledger: Ledger, *, source_id: str) -> dict[str, Any]:
    """One payment or credit memo by (canonicalized) ID, with applications."""
    canon = canonicalize_doc_id(source_id, ledger)
    if any(i.id == canon for i in ledger.invoices):
        raise ToolError(
            f"{canon} is an invoice; get_source accepts payment (PMT-*) and"
            " credit memo (CM-*) IDs only. Use get_invoice for invoices."
        )
    doc: Payment | CreditMemo | None = next(
        (p for p in ledger.payments if p.id == canon), None
    ) or next((c for c in ledger.credit_memos if c.id == canon), None)
    if doc is None:
        raise ToolError(f"unknown source {canon}")
    row = _source_row(ledger, doc)
    row["applications"] = [
        {
            "target_invoice": a.target_invoice,
            "amount": render(cents(a.amount_cents)),
            "amount_cents": a.amount_cents,
        }
        for a in ledger.applications
        if a.source_id == canon
    ]
    return row


def get_application_history(ledger: Ledger) -> dict[str, Any]:
    """Every application, plus applied totals per invoice and per source."""
    source_types = {p.id: "payment" for p in ledger.payments}
    source_types.update({c.id: "credit_memo" for c in ledger.credit_memos})
    applications = [
        {
            "source_id": a.source_id,
            "target_invoice": a.target_invoice,
            "amount": render(cents(a.amount_cents)),
            "amount_cents": a.amount_cents,
            "source_type": source_types.get(a.source_id, "unknown"),
        }
        for a in ledger.applications
    ]
    applied_by_invoice: dict[str, dict[str, Any]] = {}
    for inv in ledger.invoices:
        applied = derive.applied_to_invoice(ledger, inv.id)
        applied_by_invoice[inv.id] = {"applied": render(applied), "applied_cents": applied}
    applied_by_source: dict[str, dict[str, Any]] = {}
    for source_id in source_types:
        applied = derive.applied_from_source(ledger, source_id)
        applied_by_source[source_id] = {"applied": render(applied), "applied_cents": applied}
    return {
        "applications": applications,
        "applied_by_invoice": applied_by_invoice,
        "applied_by_source": applied_by_source,
    }


# ---------------------------------------------------------------------------
# Registry, catalog, executor
# ---------------------------------------------------------------------------


def _no_args_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _id_schema(field: str, description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {field: {"type": "string", "description": description}},
        "required": [field],
        "additionalProperties": False,
    }


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_account_summary": {
        "fn": get_account_summary,
        "schema": _no_args_schema(),
        "description": (
            "Account totals: open invoice total, unapplied cash and credit"
            " totals, net balance due, and the detected structure signature."
        ),
    },
    "list_open_invoices": {
        "fn": list_open_invoices,
        "schema": _no_args_schema(),
        "description": (
            "Every invoice with an open amount, with original, applied, and"
            " open amounts, derived status, and memo."
        ),
    },
    "get_invoice": {
        "fn": get_invoice,
        "schema": _id_schema(
            "invoice_id",
            "Invoice ID, e.g. INV-3063; tolerant forms like 'inv 3063' work.",
        ),
        "description": (
            "One invoice by ID: dates, original, applied, and open amounts,"
            " derived status, and every application targeting it."
        ),
    },
    "list_unapplied_sources": {
        "fn": list_unapplied_sources,
        "schema": _no_args_schema(),
        "description": (
            "Every payment or credit memo with money not yet applied to any"
            " invoice, with applied and unapplied amounts."
        ),
    },
    "get_source": {
        "fn": get_source,
        "schema": _id_schema(
            "source_id",
            "Payment or credit memo ID, e.g. PMT-0278 or CM-0025; not invoices.",
        ),
        "description": (
            "One payment or credit memo by ID: applied and unapplied totals"
            " and every application from it."
        ),
    },
    "get_application_history": {
        "fn": get_application_history,
        "schema": _no_args_schema(),
        "description": (
            "Every application of money onto invoices, plus applied totals"
            " grouped per invoice and per source."
        ),
    },
}


def tool_catalog() -> list[dict[str, Any]]:
    """The six tools as [{"name", "description", "parameters"}], stable order."""
    return [
        {
            "name": name,
            "description": entry["description"],
            "parameters": entry["schema"],
        }
        for name, entry in TOOL_REGISTRY.items()
    ]


def execute_tool(name: str, arguments: dict, ledger: Ledger) -> dict:
    """Validate and run one tool call; never raises for bad input.

    Returns {"ok": True, "data": {...}} on success, or
    {"ok": False, "error": "<reason>"} for an unknown tool, schema-invalid
    arguments, or a typed per-call failure such as an unknown document ID.
    """
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    try:
        jsonschema.validate(arguments, entry["schema"])
    except jsonschema.ValidationError as exc:
        return {"ok": False, "error": f"invalid arguments for {name}: {exc.message}"}
    fn: Callable[..., dict[str, Any]] = entry["fn"]
    try:
        data = fn(ledger, **arguments)
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # defensive: the executor contract is no-raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "data": data}
