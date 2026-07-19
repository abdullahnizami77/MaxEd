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

    verified = 0
    total = 0
    unmatched: list[dict[str, str]] = []

    for match in AMOUNT_RE.finditer(draft_text):
        token = match.group(0)
        total += 1
        if parse_cents(token) in truth:
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
