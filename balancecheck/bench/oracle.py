"""The harness's INDEPENDENT grounding oracle (invariant I12).

What this module guarantees:

- It imports nothing from balancecheck: only json, re, and pathlib from the
  standard library. In particular it never imports balancecheck.checks,
  balancecheck.substrate.derive, or balancecheck.drafting; the I12 test
  AST-scans this file's imports to prove it.
- Every number it trusts is recomputed here from the raw fixture JSON by its
  own replay code, and every dollar token and document ID it reads out of a
  draft is found by its own regexes and parsed by its own tiny cents parser.
  The duplication is the point: this is an independently authored path over
  the same fixture truth.

The oracle exists so a blind spot in checks/ cannot hide in the eval: if the
gate's checkers and this oracle disagree on a correct draft, that
disagreement is itself a finding (a checker bug or an oracle bug), instead
of a shared blind spot the harness silently inherits.

All money in this module is integer cents; no float literal appears here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Independently authored surface regexes: dollar amounts and document IDs.
AMOUNT_RE = re.compile(r"-?\$\d[\d,]*(?:\.\d{1,2})?")
ID_RE = re.compile(r"\b(?:INV|PMT|CM)-\d+\b")


def parse_cents(token: str) -> int:
    """Parse a dollar token like '$1,250.00' or '-$35' to integer cents.

    Deliberately written from scratch (not money.parse_dollars): dollars and
    fractional cents are read as integers, no float is ever constructed.
    """
    s = token.strip()
    negative = s.startswith("-")
    if negative:
        s = s[1:]
    if s.startswith("$"):
        s = s[1:]
    s = s.replace(",", "")
    if "." in s:
        dollars_part, cents_part = s.split(".", 1)
        cents_part = (cents_part + "00")[:2]
    else:
        dollars_part, cents_part = s, "00"
    value = int(dollars_part or "0") * 100 + int(cents_part)
    return -value if negative else value


def render_cents(value: int) -> str:
    """Render integer cents as '$1,250.00' ('-$150.00' when negative).

    Deliberately written from scratch (not money.render); integer divmod
    only, thousands grouped by hand.
    """
    sign = "-" if value < 0 else ""
    dollars, cent_rem = divmod(abs(value), 100)
    digits = str(dollars)
    groups: list[str] = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return f"{sign}${','.join(groups)}.{cent_rem:02d}"


def truth_sets(fixture: dict) -> tuple[set[int], set[str]]:
    """Replay the raw fixture dicts into (true cent values, document IDs).

    The truth set of integers contains: every document amount_cents; every
    invoice's open amount (amount minus applications targeting it); every
    payment's and credit memo's unapplied amount (amount minus applications
    from it); and the derived totals (open invoice total, unapplied cash
    total, unapplied credit total, net balance). All computed here by event
    replay over raw dicts, sharing no code with derive.py.
    """
    truth: set[int] = set()
    ids: set[str] = set()

    open_by_invoice: dict[str, int] = {}
    for inv in fixture.get("invoices", []):
        ids.add(inv["id"])
        truth.add(inv["amount_cents"])
        open_by_invoice[inv["id"]] = open_by_invoice.get(inv["id"], 0) + inv["amount_cents"]

    unapplied_by_source: dict[str, int] = {}
    payment_ids: set[str] = set()
    credit_ids: set[str] = set()
    for pay in fixture.get("payments", []):
        ids.add(pay["id"])
        payment_ids.add(pay["id"])
        truth.add(pay["amount_cents"])
        unapplied_by_source[pay["id"]] = (
            unapplied_by_source.get(pay["id"], 0) + pay["amount_cents"]
        )
    for cm in fixture.get("credit_memos", []):
        ids.add(cm["id"])
        credit_ids.add(cm["id"])
        truth.add(cm["amount_cents"])
        unapplied_by_source[cm["id"]] = (
            unapplied_by_source.get(cm["id"], 0) + cm["amount_cents"]
        )

    for app in fixture.get("applications", []):
        open_by_invoice[app["target_invoice"]] = (
            open_by_invoice.get(app["target_invoice"], 0) - app["amount_cents"]
        )
        unapplied_by_source[app["source_id"]] = (
            unapplied_by_source.get(app["source_id"], 0) - app["amount_cents"]
        )

    truth.update(open_by_invoice.values())
    truth.update(unapplied_by_source.values())

    open_invoice_total = 0
    for v in open_by_invoice.values():
        open_invoice_total += v
    unapplied_cash_total = 0
    for pid in payment_ids:
        unapplied_cash_total += unapplied_by_source.get(pid, 0)
    unapplied_credit_total = 0
    for cid in credit_ids:
        unapplied_credit_total += unapplied_by_source.get(cid, 0)
    net_balance = open_invoice_total - unapplied_cash_total - unapplied_credit_total

    truth.update(
        {open_invoice_total, unapplied_cash_total, unapplied_credit_total, net_balance}
    )
    # Zero-cent replayed values (a fully applied source, a paid-off invoice)
    # would let a fabricated "$0.00" claim verify on any ledger. Zero is only
    # a true figure when the net balance itself is zero.
    if net_balance != 0:
        truth.discard(0)
    return truth, ids


def document_figures(fixture: dict) -> tuple[dict[str, set[int]], set[int]]:
    """Per-document true cent values, and the set of account-level totals.

    Independently replayed (no shared code): each invoice maps to its
    original and open amounts; each payment/credit to its original and
    unapplied amounts. The totals set holds the open-invoice total, the
    unapplied cash and credit totals, and the net balance. Binding an amount
    to the document its sentence names, and checking it against THAT
    document's figures, is what makes the oracle a grounding check rather
    than a global-token-membership check.
    """
    figures: dict[str, set[int]] = {}
    open_by_invoice: dict[str, int] = {}
    for inv in fixture.get("invoices", []):
        open_by_invoice[inv["id"]] = open_by_invoice.get(inv["id"], 0) + inv["amount_cents"]
    unapplied_by_source: dict[str, int] = {}
    payment_ids: set[str] = set()
    credit_ids: set[str] = set()
    originals: dict[str, int] = {}
    for pay in fixture.get("payments", []):
        payment_ids.add(pay["id"])
        originals[pay["id"]] = pay["amount_cents"]
        unapplied_by_source[pay["id"]] = unapplied_by_source.get(pay["id"], 0) + pay["amount_cents"]
    for cm in fixture.get("credit_memos", []):
        credit_ids.add(cm["id"])
        originals[cm["id"]] = cm["amount_cents"]
        unapplied_by_source[cm["id"]] = unapplied_by_source.get(cm["id"], 0) + cm["amount_cents"]
    for inv in fixture.get("invoices", []):
        originals[inv["id"]] = inv["amount_cents"]
    for app in fixture.get("applications", []):
        open_by_invoice[app["target_invoice"]] = (
            open_by_invoice.get(app["target_invoice"], 0) - app["amount_cents"]
        )
        unapplied_by_source[app["source_id"]] = (
            unapplied_by_source.get(app["source_id"], 0) - app["amount_cents"]
        )
    for inv in fixture.get("invoices", []):
        figures[inv["id"]] = {originals[inv["id"]], open_by_invoice[inv["id"]]}
    for src_id in payment_ids | credit_ids:
        figures[src_id] = {originals[src_id], unapplied_by_source[src_id]}

    open_invoice_total = sum(open_by_invoice.values())
    unapplied_cash_total = sum(unapplied_by_source.get(p, 0) for p in payment_ids)
    unapplied_credit_total = sum(unapplied_by_source.get(c, 0) for c in credit_ids)
    net_balance = open_invoice_total - unapplied_cash_total - unapplied_credit_total
    totals = {open_invoice_total, unapplied_cash_total, unapplied_credit_total, net_balance}
    if net_balance != 0:
        totals.discard(0)
    return figures, totals


def _sentences(text: str) -> list[str]:
    # Split on sentence enders followed by whitespace (or newlines), so a
    # decimal point inside an amount like "$12.34" is not a sentence break.
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def oracle_grounding(draft_text: str, fixture_path: Path) -> dict:
    """Score a draft's dollar amounts and document IDs against fixture truth.

    Returns {"verified": int, "total": int, "unmatched": [{"token", "kind"}]}
    where each $-amount in the draft verifies iff its cents value is in the
    replayed truth set, and each (INV|PMT|CM)-<digits> ID verifies iff it is
    in the fixture's document ID set. Unmatched leftovers are returned with
    kind "amount" or "id" so a divergence from the gate's checkers can be
    inspected token by token.
    """
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    truth, ids = truth_sets(fixture)
    figures, totals = document_figures(fixture)

    verified = 0
    total = 0
    unmatched: list[dict[str, str]] = []

    # Amounts are bound to the document their sentence names: an amount in a
    # sentence with exactly one known document must be one of THAT document's
    # figures; an amount with no document must be an account-level total.
    # This is what stops a real amount attributed to the wrong invoice from
    # verifying by global membership.
    for sentence in _sentences(draft_text):
        sentence_ids = [t for t in ID_RE.findall(sentence) if t in ids]
        for match in AMOUNT_RE.finditer(sentence):
            token = match.group(0)
            total += 1
            value = parse_cents(token)
            if len(sentence_ids) == 1:
                ok = value in figures.get(sentence_ids[0], set())
            elif not sentence_ids:
                ok = value in totals
            else:
                # Multiple documents named: the amount must be a figure of one
                # of them or an account total.
                ok = value in totals or any(value in figures.get(d, set()) for d in sentence_ids)
            if ok:
                verified += 1
            else:
                unmatched.append({"token": token, "kind": "amount"})

    for match in ID_RE.finditer(draft_text):
        token = match.group(0)
        total += 1
        if token in ids:
            verified += 1
        else:
            unmatched.append({"token": token, "kind": "id"})

    return {"verified": verified, "total": total, "unmatched": unmatched}
