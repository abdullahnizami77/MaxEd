"""The check registry: every claim type routed to the cheapest layer that can check it.

What this module guarantees:

- Every code-checkable claim type (C-EXIST, C-AMT, C-SUM, C-STATUS, C-DATE)
  is resolved by pure code against derive.py truth; run_code_checks never
  touches a model client, so invariant I3 holds by construction and the
  routing test proves it with a client object that raises on any attribute
  access.
- A claim arriving with check_result already set (the UNVERIFIABLE spans the
  surface extractor pre-marks, invariant I11) is never overwritten; the
  escalate-not-drop signal survives this layer untouched.
- The one model-checked type, C-FUZZY, is verified in isolation (invariant
  I4): the prompt built here contains exactly one claim sentence and a
  locally rendered records block, never the surrounding draft and never a
  sibling claim. Every record ID the verifier cites is re-verified by code
  against the ledger; a citation of a nonexistent record downgrades the
  verdict to CANNOT_DETERMINE, so the model cannot launder a fabrication
  through its own citations.
- Amount tokens resolve through one private seam (_parse_amount) that
  prefers drafting.surface.parse_amount_token and falls back to
  money.parse_dollars, so the integrator has a single place to look.

All money in this module is integer cents; no float literal appears here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from balancecheck.config import REPO_ROOT
from balancecheck.contracts.models import (
    CODE_CHECKED_TYPES,
    AmountRole,
    CheckResult,
    CheckStatus,
    Claim,
    ClaimType,
    InvoiceStatus,
    Ledger,
)
from balancecheck.substrate import derive
from balancecheck.substrate.money import Cents, cents, parse_dollars, render

VERIFY_TEMPLATE_PATH: Path = REPO_ROOT / "prompts" / "verify_claim.md"

MODEL_CHECKED_TYPES = frozenset({ClaimType.C_FUZZY})

# Labels cited for matches against derived totals (not document IDs, but the
# reader of a check report needs to know which computed figure matched).
NET_LABEL = "NET"
_TOTAL_LABELS: tuple[tuple[str, Callable[[Ledger], Cents]], ...] = (
    ("OPEN_INVOICE_TOTAL", derive.open_invoice_total),
    ("UNAPPLIED_CASH_TOTAL", derive.unapplied_cash_total),
    ("UNAPPLIED_CREDIT_TOTAL", derive.unapplied_credit_total),
    (NET_LABEL, derive.net_balance),
)

# The constrained-decoding schema for the fuzzy verifier's verdict.
FUZZY_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["supported", "unsupported", "cannot_determine"],
        },
        "cited_record_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "cited_record_ids", "reason"],
    "additionalProperties": False,
}

_VERDICT_TO_STATUS: dict[str, CheckStatus] = {
    "supported": CheckStatus.PASS,
    "unsupported": CheckStatus.UNSUPPORTED,
    "cannot_determine": CheckStatus.CANNOT_DETERMINE,
}

_STATUS_WORD_MAP: dict[str, InvoiceStatus] = {
    "paid": InvoiceStatus.PAID,
    "settled": InvoiceStatus.PAID,
    "cleared": InvoiceStatus.PAID,
    "outstanding": InvoiceStatus.OUTSTANDING,
    "unpaid": InvoiceStatus.OUTSTANDING,
    "open": InvoiceStatus.OUTSTANDING,
    "overdue": InvoiceStatus.OVERDUE,
    "past due": InvoiceStatus.OVERDUE,
}

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_DATE_RE = re.compile(
    rf"({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.IGNORECASE
)
_SLASH_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


class CompletesStructured(Protocol):
    """The one client capability the fuzzy check needs (duck-typed in tests)."""

    def complete(
        self,
        task: str,
        prompt: str,
        *,
        system: str = "",
        schema: dict | None = None,
        stub_key: str = "",
        meta: dict | None = None,
        temperature: float = 0,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# The single amount-parsing seam
# ---------------------------------------------------------------------------


_OPEN_CONTEXT_RE = re.compile(
    r"\b(?:open|remaining|outstanding|unpaid|still\s+due|due|owe[sd]?|left)\b",
    re.IGNORECASE,
)
# A decomposition sentence ("originally $X, of which $Y has been paid,
# leaving $Z open") legitimately carries the original and applied amounts
# alongside the open one; condemning them would reject the most transparent
# partial-payment phrasing there is.
_DECOMPOSITION_RE = re.compile(
    r"\b(?:originally|original\s+amount|of\s+which|leaving|partial(?:ly)?)\b",
    re.IGNORECASE,
)
_REMIT_CONTEXT_RE = re.compile(
    r"\b(?:remit|send|wire|transfer|please\s+pay|pay\s+us|kindly\s+(?:send|wire|pay))\b",
    re.IGNORECASE,
)
_RECEIVED_CONTEXT_RE = re.compile(
    r"\b(?:received?|we\s+got|thank\s+you\s+for\s+your\s+payment)\b",
    re.IGNORECASE,
)


def _no_subject_context(span: str) -> str:
    """Classify what an unattributed amount sentence asserts.

    A received-family word wins when both appear ("we received your transfer
    of $600"): the factual assertion is that money came in, and checking it
    against payments is the honest test.
    """
    if _RECEIVED_CONTEXT_RE.search(span):
        return "received"
    if _REMIT_CONTEXT_RE.search(span):
        return "remit"
    return "plain"


def parse_claim_amount(token: str) -> Cents:
    """Public parse for cross-claim consumers (gate policy's aggregate rows)."""
    return _parse_amount(token)


def _parse_amount(token: str) -> Cents:
    """Resolve an amount token to integer cents through one seam.

    Prefers drafting.surface.parse_amount_token (which also resolves
    "2,400 dollars" and number-word forms). A ValueError is the documented
    "unresolvable" signal from either parser and propagates; an import
    failure or any other runtime failure falls back to money.parse_dollars.
    """
    try:
        from balancecheck.drafting.surface import parse_amount_token

        return parse_amount_token(token)
    except ValueError:
        raise
    except Exception:
        return parse_dollars(token)


def _parse_date_token(token: str) -> date:
    """Resolve a date token (ISO, month-name, or M/D/YYYY) to a date.

    Local on purpose: the checks layer must not depend on the drafting
    package for its date semantics. Raises ValueError on anything else.
    """
    s = token.strip().strip(".,;:").strip()
    if _ISO_DATE_RE.fullmatch(s):
        return date.fromisoformat(s)
    m = _MONTH_DATE_RE.fullmatch(s)
    if m:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    m = _SLASH_DATE_RE.fullmatch(s)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    raise ValueError(f"unparseable date token: {token!r}")


# ---------------------------------------------------------------------------
# Ledger document lookup (local; derive's helpers raise, this one reports)
# ---------------------------------------------------------------------------


def _find_document(ledger: Ledger, doc_id: str) -> tuple[str, Any]:
    for inv in ledger.invoices:
        if inv.id == doc_id:
            return "invoice", inv
    for pay in ledger.payments:
        if pay.id == doc_id:
            return "payment", pay
    for cm in ledger.credit_memos:
        if cm.id == doc_id:
            return "credit_memo", cm
    return "", None


def _dedup(values: list[Cents]) -> list[Cents]:
    seen: set[int] = set()
    out: list[Cents] = []
    for v in values:
        if int(v) not in seen:
            seen.add(int(v))
            out.append(v)
    return out


def _bound_docs(claim: Claim) -> list[str]:
    """The documents a claim is bound to: the extractor's subject_ids, or the
    single-subject back-compat field when subject_ids was not populated."""
    if claim.subject_ids:
        return list(claim.subject_ids)
    return [claim.subject_id] if claim.subject_id else []


# A less specific but still true status word passes; a falsely stronger one
# fails. An overdue invoice is also unpaid/outstanding/open in ordinary usage,
# so those weaker words are accepted; calling an outstanding invoice "overdue"
# or an open one "paid" is not.
_STATUS_COMPAT: dict[InvoiceStatus, set[str]] = {
    InvoiceStatus.PAID: {"paid", "settled", "cleared"},
    InvoiceStatus.OUTSTANDING: {"outstanding", "unpaid", "open"},
    InvoiceStatus.OVERDUE: {"overdue", "past due", "outstanding", "unpaid", "open"},
}
_KNOWN_STATUS_WORDS: frozenset[str] = frozenset().union(*_STATUS_COMPAT.values())


# ---------------------------------------------------------------------------
# Code checkers (invariant I3: no model anywhere below)
# ---------------------------------------------------------------------------


def check_exist(claim: Claim, ledger: Ledger) -> CheckResult:
    """C-EXIST: the referenced document ID is present in the ledger."""
    if claim.token in derive.document_ids(ledger):
        return CheckResult(
            status=CheckStatus.PASS,
            expected=claim.token,
            actual=claim.token,
            cited_records=[claim.token],
        )
    return CheckResult(
        status=CheckStatus.FAIL,
        actual=claim.token,
        detail="no such document in the ledger",
    )


def check_amount(claim: Claim, ledger: Ledger) -> CheckResult:
    """C-AMT: the amount equals a figure the ledger actually supports."""
    try:
        claimed = _parse_amount(claim.token)
    except ValueError as exc:
        return CheckResult(
            status=CheckStatus.UNVERIFIABLE,
            actual=claim.token,
            detail=f"amount token could not be resolved to cents: {exc}",
        )
    shown = render(claimed)

    bound = _bound_docs(claim)
    if len(bound) == 1:
        subject = bound[0]
        kind, doc = _find_document(ledger, subject)
        if doc is None:
            return CheckResult(
                status=CheckStatus.FAIL,
                expected=render(derive.net_balance(ledger)),
                actual=shown,
                detail=f"amount attached to {subject}, which is not in the ledger",
            )
        # The extractor bound the amount to a role (the ledger figure the
        # phrase names). Verify exactly that figure: an "open" amount must be
        # the open figure, an "original" amount the invoice's face value, an
        # "applied" amount the paid portion. A bare amount (no role word)
        # accepts either the document amount or its open/unapplied figure.
        if kind == "invoice":
            original = cents(doc.amount_cents)
            open_amt = derive.open_amount(ledger, doc.id)
            applied = derive.applied_to_invoice(ledger, doc.id)
            by_role = {
                AmountRole.OPEN: [open_amt],
                AmountRole.ORIGINAL: [original],
                AmountRole.APPLIED: [applied],
                AmountRole.UNKNOWN: [original, open_amt],
            }
        else:
            original = cents(doc.amount_cents)
            unapplied = derive.unapplied_amount(ledger, doc.id)
            applied = derive.applied_from_source(ledger, doc.id)
            by_role = {
                AmountRole.OPEN: [unapplied],
                AmountRole.ORIGINAL: [original],
                AmountRole.APPLIED: [applied],
                AmountRole.UNKNOWN: [original, unapplied],
            }
        acceptable = _dedup(by_role.get(claim.role, by_role[AmountRole.UNKNOWN]))
        if claimed in acceptable:
            return CheckResult(
                status=CheckStatus.PASS,
                expected=shown,
                actual=shown,
                cited_records=[subject],
            )
        role_note = f" ({claim.role})" if claim.role else ""
        return CheckResult(
            status=CheckStatus.FAIL,
            expected=" or ".join(render(v) for v in acceptable),
            actual=shown,
            cited_records=[subject],
            detail=f"amount does not match {subject}{role_note}",
        )

    # No subject: the sentence attaches this amount to nothing checkable, so
    # what it asserts decides how strict we are. A coincidental match against
    # some unrelated ledger row is NOT support (the adversarial red team
    # walked materially wrong drafts through exactly that leniency).
    net = derive.net_balance(ledger)
    context = _no_subject_context(claim.span)
    if context == "remit":
        # The draft is telling the client what to pay: only the net balance
        # is a defensible unattributed figure.
        if claimed == net:
            return CheckResult(
                status=CheckStatus.PASS,
                expected=shown,
                actual=shown,
                cited_records=[NET_LABEL],
            )
        return CheckResult(
            status=CheckStatus.FAIL,
            expected=render(net),
            actual=shown,
            cited_records=[NET_LABEL],
            detail="asks the client to pay an amount that is not the net balance",
        )
    if context == "received":
        # The draft asserts money came in: the figure must match a payment,
        # an unapplied payment amount, or the total received.
        acceptable: list[Cents] = []
        for pay in ledger.payments:
            acceptable.append(cents(pay.amount_cents))
            acceptable.append(derive.unapplied_amount(ledger, pay.id))
        acceptable.append(cents(sum(p.amount_cents for p in ledger.payments)))
        matched = [
            pay.id
            for pay in ledger.payments
            if claimed
            in (cents(pay.amount_cents), derive.unapplied_amount(ledger, pay.id))
        ]
        if claimed in _dedup(acceptable):
            return CheckResult(
                status=CheckStatus.PASS,
                expected=shown,
                actual=shown,
                cited_records=matched or ["PAYMENTS-TOTAL"],
            )
        return CheckResult(
            status=CheckStatus.FAIL,
            expected="a recorded payment amount",
            actual=shown,
            detail="asserts a received amount matching no payment on the ledger",
        )
    # Plain context: an unattributed figure. Restating the net balance is
    # fine; restating the open-invoice total is the classic ignores-unapplied
    # misreading; anything else is unverifiable and escalates (I11 posture),
    # never silently passed on a coincidental match.
    if claimed == net:
        return CheckResult(
            status=CheckStatus.PASS,
            expected=shown,
            actual=shown,
            cited_records=[NET_LABEL],
        )
    open_total = derive.open_invoice_total(ledger)
    if claimed == open_total and open_total != net:
        return CheckResult(
            status=CheckStatus.FAIL,
            expected=render(net),
            actual=shown,
            cited_records=[NET_LABEL],
            detail="states the open-invoice total and ignores unapplied cash or credits",
        )
    return CheckResult(
        status=CheckStatus.UNVERIFIABLE,
        actual=shown,
        detail=(
            "amount is attached to no document and asserts no checkable total;"
            " escalated rather than matched against unrelated ledger figures"
        ),
    )


def check_sum(claim: Claim, ledger: Ledger) -> CheckResult:
    """C-SUM: the stated total equals the computed net balance."""
    try:
        claimed = _parse_amount(claim.token)
    except ValueError as exc:
        return CheckResult(
            status=CheckStatus.UNVERIFIABLE,
            actual=claim.token,
            detail=f"total token could not be resolved to cents: {exc}",
        )
    net = derive.net_balance(ledger)
    bound_invoices = [d for d in _bound_docs(claim) if _find_document(ledger, d)[0] == "invoice"]
    if bound_invoices:
        # A subtotal over specific named invoices: verify against THOSE
        # invoices' open amounts, not a bare equality with the account net.
        subtotal = cents(sum(derive.open_amount(ledger, d) for d in bound_invoices))
        if claimed == subtotal:
            return CheckResult(
                status=CheckStatus.PASS,
                expected=render(subtotal),
                actual=render(claimed),
                cited_records=bound_invoices,
                detail="a true subtotal over the named invoices",
            )
        return CheckResult(
            status=CheckStatus.FAIL,
            expected=render(subtotal),
            actual=render(claimed),
            cited_records=bound_invoices,
            detail="does not equal the open total of the named invoices",
        )

    if claimed == net:
        return CheckResult(
            status=CheckStatus.PASS,
            expected=render(net),
            actual=render(claimed),
            cited_records=[NET_LABEL],
        )
    open_total = derive.open_invoice_total(ledger)
    if claimed == open_total and open_total != net:
        # A subtotal predicated of the open invoices/line items is a true
        # statement ("your two open invoices total $8,450.00") and passes;
        # the same figure presented as the account balance is the classic
        # ignores-unapplied misreading and fails.
        if re.search(r"\b(?:invoices?|line\s+items?|open\s+items?)\b", claim.span, re.IGNORECASE):
            open_ids = [
                inv.id
                for inv in ledger.invoices
                if derive.open_amount(ledger, inv.id) > 0
            ]
            return CheckResult(
                status=CheckStatus.PASS,
                expected=render(claimed),
                actual=render(claimed),
                cited_records=open_ids,
                detail="a true open-invoice subtotal, distinct from the net balance",
            )
        detail = "states the open-invoice total and ignores unapplied cash or credits"
    else:
        detail = "does not match the computed balance"
    return CheckResult(
        status=CheckStatus.FAIL,
        expected=render(net),
        actual=render(claimed),
        cited_records=[NET_LABEL],
        detail=detail,
    )


def check_status(claim: Claim, ledger: Ledger) -> CheckResult:
    """C-STATUS: the claimed status word is compatible with the derived status.

    A less specific but still true word passes (an overdue invoice is also
    unpaid/outstanding); a falsely stronger word fails.
    """
    bound = _bound_docs(claim)
    subject = bound[0] if len(bound) == 1 else ""
    if not subject:
        # A status asserted of no resolvable document is not provably wrong,
        # it is unverifiable: escalate to a human rather than condemn a
        # possibly-true sentence with a definitive FAIL.
        return CheckResult(
            status=CheckStatus.UNVERIFIABLE,
            actual=claim.token,
            detail="status claim with no resolvable document",
        )
    kind, _doc = _find_document(ledger, subject)
    if kind != "invoice":
        return CheckResult(
            status=CheckStatus.UNVERIFIABLE,
            actual=claim.token,
            detail=f"status claim about {subject}, which is not an invoice",
        )
    derived = derive.invoice_status(ledger, subject)
    word = " ".join(claim.token.lower().split())
    if word not in _KNOWN_STATUS_WORDS:
        return CheckResult(
            status=CheckStatus.FAIL,
            expected=derived.value,
            actual=claim.token,
            cited_records=[subject],
            detail=f"unrecognized status word {claim.token!r}",
        )
    if word in _STATUS_COMPAT[derived]:
        return CheckResult(
            status=CheckStatus.PASS,
            expected=derived.value,
            actual=claim.token,
            cited_records=[subject],
        )
    return CheckResult(
        status=CheckStatus.FAIL,
        expected=derived.value,
        actual=claim.token,
        cited_records=[subject],
        detail=f"the ledger derives {derived.value} for {subject}",
    )


def check_date(claim: Claim, ledger: Ledger) -> CheckResult:
    """C-DATE: the claimed date is one the ledger actually carries.

    Matching runs first, so a due date later than as_of that the ledger
    itself states still passes; the as_of rule condemns only claimed dates
    the ledger does not carry (a fabricated future date is the target).
    """
    try:
        claimed = _parse_date_token(claim.token)
    except ValueError as exc:
        return CheckResult(
            status=CheckStatus.UNVERIFIABLE,
            actual=claim.token,
            detail=f"date token could not be resolved: {exc}",
        )
    as_of = ledger.client.as_of

    bound = [d for d in _bound_docs(claim) if _find_document(ledger, d)[1] is not None]
    if bound:
        # The extractor bound this date to the document(s) the sentence
        # attributes it to; it must be a date EVERY bound document actually
        # carries. A date true for one named invoice but stated for two does
        # not pass by proximity.
        def carries(doc_id: str) -> bool:
            kind, doc = _find_document(ledger, doc_id)
            dates = (doc.date, doc.due) if kind == "invoice" else (doc.date,)
            return claimed in dates

        missing = [d for d in bound if not carries(d)]
        if not missing:
            return CheckResult(
                status=CheckStatus.PASS,
                expected=claimed.isoformat(),
                actual=claim.token,
                cited_records=bound,
            )
        detail = (
            "date is after the statement as_of"
            if claimed > as_of
            else f"date is not carried by {', '.join(missing)}"
        )
        return CheckResult(
            status=CheckStatus.FAIL,
            expected=", ".join(sorted({d.date.isoformat() for d in [_find_document(ledger, b)[1] for b in bound]})),
            actual=claim.token,
            cited_records=bound,
            detail=detail,
        )

    # No single bound document: a genuinely unattributed date (e.g. the
    # statement as_of). Accept only dates the ledger actually carries.
    matches: list[str] = []
    for inv in ledger.invoices:
        if claimed in (inv.date, inv.due):
            matches.append(inv.id)
    for pay in ledger.payments:
        if claimed == pay.date:
            matches.append(pay.id)
    for cm in ledger.credit_memos:
        if claimed == cm.date:
            matches.append(cm.id)
    if claimed == as_of:
        matches.append("AS_OF")
    if matches:
        return CheckResult(
            status=CheckStatus.PASS,
            expected=claimed.isoformat(),
            actual=claim.token,
            cited_records=matches,
        )
    if claimed > as_of:
        detail = "date is after the statement as_of"
    else:
        detail = "date matches no document date, due date, or the statement as_of"
    return CheckResult(
        status=CheckStatus.FAIL,
        expected="no such date in the ledger; remove it or cite a document date",
        actual=claim.token,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# The isolated fuzzy check (invariant I4)
# ---------------------------------------------------------------------------


def render_records_block(ledger: Ledger) -> str:
    """A minimal, deterministic records block for the verifier prompt.

    Local on purpose: the verifier must not import the drafter's renderer,
    so a drafter prompt change can never leak draft context into the
    verification path. Every figure is computed by derive.py and rendered
    by money.render.
    """
    c = ledger.client
    lines: list[str] = [f"CLIENT {c.id} ({c.name}) | as_of {c.as_of.isoformat()}"]
    for inv in ledger.invoices:
        lines.append(
            f"INVOICE {inv.id} | date {inv.date.isoformat()} | due {inv.due.isoformat()}"
            f" | amount {render(cents(inv.amount_cents))}"
            f" | open {render(derive.open_amount(ledger, inv.id))}"
            f" | status {derive.invoice_status(ledger, inv.id).value}"
            + (f" | memo: {inv.memo}" if inv.memo else "")
        )
    for pay in ledger.payments:
        lines.append(
            f"PAYMENT {pay.id} | date {pay.date.isoformat()}"
            f" | amount {render(cents(pay.amount_cents))}"
            f" | unapplied {render(derive.unapplied_amount(ledger, pay.id))}"
            f" | method {pay.method}"
            + (f" | reference {pay.reference}" if pay.reference else "")
        )
    for cm in ledger.credit_memos:
        lines.append(
            f"CREDIT MEMO {cm.id} | date {cm.date.isoformat()}"
            f" | amount {render(cents(cm.amount_cents))}"
            f" | unapplied {render(derive.unapplied_amount(ledger, cm.id))}"
            + (f" | reason: {cm.reason}" if cm.reason else "")
        )
    for app in ledger.applications:
        lines.append(
            f"APPLIED {app.source_id} -> {app.target_invoice}:"
            f" {render(cents(app.amount_cents))}"
        )
    lines.append(f"NET BALANCE DUE {render(derive.net_balance(ledger))}")
    return "\n".join(lines)


def _load_verify_template(template_path: Path | None = None) -> tuple[str, str]:
    path = template_path or VERIFY_TEMPLATE_PATH
    text = path.read_text(encoding="utf-8")
    if "[SYSTEM]" not in text or "[USER]" not in text:
        raise ValueError(f"template {path} must contain [SYSTEM] and [USER] sections")
    _, _, rest = text.partition("[SYSTEM]")
    system_part, _, user_part = rest.partition("[USER]")
    return system_part.strip(), user_part.strip()


def build_fuzzy_prompt(
    claim: Claim, ledger: Ledger, *, template_path: Path | None = None
) -> tuple[str, str]:
    """Assemble the (system, user) prompt pair for one fuzzy verification.

    The user prompt contains exactly one claim sentence (claim.span) and the
    locally rendered records block. No draft text, no sibling claims, no
    drafter self-report can appear here, because none of them is an input.
    """
    system, user = _load_verify_template(template_path)
    user = user.replace("{{claim_sentence}}", claim.span)
    user = user.replace("{{ledger_records}}", render_records_block(ledger))
    return system, user


def check_fuzzy(
    claim: Claim,
    ledger: Ledger,
    client: CompletesStructured,
    stub_key_prefix: str = "",
) -> CheckResult:
    """C-FUZZY: model verdict on one isolated sentence, citations code-verified."""
    system, user = build_fuzzy_prompt(claim, ledger)
    resp = client.complete(
        task="verify_fuzzy",
        prompt=user,
        system=system,
        schema=FUZZY_VERDICT_SCHEMA,
        stub_key=f"{stub_key_prefix}:fuzzy:{claim.claim_id}",
        meta={"claim_id": claim.claim_id},
        temperature=0,
    )
    if not resp.ok or not isinstance(resp.parsed, dict):
        return CheckResult(
            status=CheckStatus.CANNOT_DETERMINE,
            actual=claim.span,
            detail=f"verifier call failed: {resp.error or 'no structured output'}",
        )
    verdict = str(resp.parsed["verdict"])
    cited = [str(r) for r in resp.parsed.get("cited_record_ids", [])]
    reason = str(resp.parsed.get("reason", ""))
    known = derive.document_ids(ledger)
    valid = [r for r in cited if r in known]
    invalid = [r for r in cited if r not in known]
    if invalid:
        return CheckResult(
            status=CheckStatus.CANNOT_DETERMINE,
            actual=claim.span,
            cited_records=valid,
            detail="verifier cited a nonexistent record: " + ", ".join(invalid),
        )
    return CheckResult(
        status=_VERDICT_TO_STATUS[verdict],
        actual=claim.span,
        cited_records=valid,
        detail=reason,
    )


# ---------------------------------------------------------------------------
# The registry and the runners
# ---------------------------------------------------------------------------

REGISTRY: dict[ClaimType, Callable[..., CheckResult]] = {
    ClaimType.C_EXIST: check_exist,
    ClaimType.C_AMT: check_amount,
    ClaimType.C_SUM: check_sum,
    ClaimType.C_STATUS: check_status,
    ClaimType.C_DATE: check_date,
    ClaimType.C_FUZZY: check_fuzzy,
}


def run_code_checks(claims: list[Claim], ledger: Ledger) -> list[Claim]:
    """Fill check_result for every code-checkable claim; touch no model.

    Claims whose check_result is already set (the surface extractor's
    UNVERIFIABLE escalations) are skipped, never overwritten.
    """
    for claim in claims:
        if claim.type not in CODE_CHECKED_TYPES:
            continue
        if claim.check_result is not None:
            continue
        claim.check_result = REGISTRY[claim.type](claim, ledger)
    return claims


def run_fuzzy_checks(
    claims: list[Claim],
    ledger: Ledger,
    client: CompletesStructured,
    stub_key_prefix: str = "",
) -> list[Claim]:
    """Fill check_result for every C-FUZZY claim via the isolated verifier."""
    for claim in claims:
        if claim.type is not ClaimType.C_FUZZY:
            continue
        if claim.check_result is not None:
            continue
        claim.check_result = check_fuzzy(claim, ledger, client, stub_key_prefix)
    return claims


def check_all(
    claims: list[Claim],
    ledger: Ledger,
    client: CompletesStructured,
    stub_key_prefix: str = "",
) -> list[Claim]:
    """Run the code layer, then the isolated fuzzy layer. The client is only
    ever touched when a C-FUZZY claim needs a verdict."""
    run_code_checks(claims, ledger)
    run_fuzzy_checks(claims, ledger, client, stub_key_prefix)
    return claims
