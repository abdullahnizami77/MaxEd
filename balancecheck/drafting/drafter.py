"""Prompt assembly and draft generation for the balance-due reply.

What this module guarantees:

- The drafter transcribes numbers, it never computes them. Every figure the
  model is allowed to state (amounts, applied and open portions, totals, the
  net balance due) is computed by substrate/derive.py and rendered into the
  ledger block by substrate/money.render before the model sees anything.
  This is the small-model-reality design: trust rides on the computed block
  and on downstream checks, not on the drafter's arithmetic.
- render_ledger_block is deterministic: the same Ledger always renders to
  the same text, so prompt hashes are attributable across runs.
- The prompt template lives in prompts/draft_reply.md (versioned, diffable,
  hash-attributable); this module only fills its placeholders. When the
  exemplars or correction blocks are empty, their placeholders collapse to
  nothing: no header, no leftover markers, no stray blank lines.
- generate_draft makes exactly one logical model call via
  ModelClient.complete(task="draft", ...) at temperature 0, so the trace
  discipline (invariant I6) is inherited from the client, and the returned
  prompt_sha matches the trace line for the same call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from balancecheck.config import REPO_ROOT
from balancecheck.contracts.models import Ledger, MemoryEntry
from balancecheck.model_client import ModelClient
from balancecheck.spine.trace import prompt_sha as compute_prompt_sha
from balancecheck.substrate import derive
from balancecheck.substrate.money import cents, render

DRAFT_TEMPLATE_PATH: Path = REPO_ROOT / "prompts" / "draft_reply.md"

# Section markers are line-anchored so prose mentions of the markers (for
# example in the template's own header comment) never split the file.
_SECTION_RE = re.compile(r"^\[(SYSTEM|USER)\][ \t]*$", re.MULTILINE)

_EXEMPLARS_HEADER = "Examples of replies our reviewers approved for similar accounts:"
_CORRECTION_HEADER = "REVISION REQUIRED. Your previous draft failed verification:"

_BLANK_RUN_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class DraftResult:
    """One generated draft with the exact prompts that produced it."""

    text: str
    system: str
    prompt: str
    prompt_sha: str


# ---------------------------------------------------------------------------
# The ledger block: every number the draft may state, pre-rendered by code
# ---------------------------------------------------------------------------


def render_ledger_block(ledger: Ledger) -> str:
    """Render the ledger as a compact deterministic plain-text table.

    Every number is rendered with money.render; every derived figure
    (applied, open, unapplied, totals, net balance) comes from derive.py.
    The drafter copies from this block; it never computes.
    """
    c = ledger.client
    lines: list[str] = [
        f"CLIENT: {c.name} ({c.id}) | terms: net {c.terms_days} | as_of {c.as_of.isoformat()}"
    ]

    lines.append("INVOICES:")
    if ledger.invoices:
        for inv in ledger.invoices:
            applied = derive.applied_to_invoice(ledger, inv.id)
            open_amt = derive.open_amount(ledger, inv.id)
            status = derive.invoice_status(ledger, inv.id).value
            lines.append(
                f"  {inv.id} | date {inv.date.isoformat()} | due {inv.due.isoformat()}"
                f" | amount {render(cents(inv.amount_cents))}"
                f" | applied {render(applied)}"
                f" | open {render(open_amt)}"
                f" | {status}"
            )
    else:
        lines.append("  (none)")

    lines.append("PAYMENTS:")
    if ledger.payments:
        for pay in ledger.payments:
            applied = derive.applied_from_source(ledger, pay.id)
            unapplied = derive.unapplied_amount(ledger, pay.id)
            lines.append(
                f"  {pay.id} | date {pay.date.isoformat()}"
                f" | amount {render(cents(pay.amount_cents))}"
                f" | applied {render(applied)}"
                f" | unapplied {render(unapplied)}"
            )
    else:
        lines.append("  (none)")

    lines.append("CREDIT MEMOS:")
    if ledger.credit_memos:
        for cm in ledger.credit_memos:
            applied = derive.applied_from_source(ledger, cm.id)
            unapplied = derive.unapplied_amount(ledger, cm.id)
            lines.append(
                f"  {cm.id} | date {cm.date.isoformat()}"
                f" | amount {render(cents(cm.amount_cents))}"
                f" | applied {render(applied)}"
                f" | unapplied {render(unapplied)}"
            )
    else:
        lines.append("  (none)")

    lines.append("APPLICATIONS:")
    if ledger.applications:
        for app in ledger.applications:
            lines.append(
                f"  {app.source_id} -> {app.target_invoice}: {render(cents(app.amount_cents))}"
            )
    else:
        lines.append("  (none)")

    lines.append("TOTALS:")
    lines.append(f"  open invoice total: {render(derive.open_invoice_total(ledger))}")
    lines.append(f"  unapplied cash total: {render(derive.unapplied_cash_total(ledger))}")
    lines.append(f"  unapplied credit total: {render(derive.unapplied_credit_total(ledger))}")
    lines.append(f"  NET BALANCE DUE: {render(derive.net_balance(ledger))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _load_template(template_path: Path | None = None) -> tuple[str, str]:
    """Split the template file into its (system, user) sections."""
    path = template_path or DRAFT_TEMPLATE_PATH
    text = path.read_text(encoding="utf-8")
    markers = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        sections[m.group(1)] = text[m.end() : end].strip()
    if "SYSTEM" not in sections or "USER" not in sections:
        raise ValueError(f"template {path} must contain [SYSTEM] and [USER] section lines")
    return sections["SYSTEM"], sections["USER"]


def _exemplars_block(exemplars: list[MemoryEntry]) -> str:
    if not exemplars:
        return ""
    parts: list[str] = [_EXEMPLARS_HEADER]
    for n, entry in enumerate(exemplars, start=1):
        parts.append(f"--- EXAMPLE {n} START ---")
        parts.append(entry.final_text.strip())
        parts.append(f"--- EXAMPLE {n} END ---")
    return "\n".join(parts)


def _correction_block(correction: str) -> str:
    if not correction:
        return ""
    return (
        _CORRECTION_HEADER
        + "\n"
        + correction.strip()
        + "\nFix ONLY those errors and change nothing else in the draft."
    )


def build_draft_prompt(
    ledger: Ledger,
    exemplars: list[MemoryEntry],
    correction: str = "",
    *,
    template_path: Path | None = None,
) -> tuple[str, str]:
    """Assemble the (system, user) prompt pair for one draft attempt.

    Empty exemplars and empty correction collapse their placeholders to
    nothing; runs of blank lines left behind are squeezed to one blank line.
    """
    system, user = _load_template(template_path)
    user = user.replace("{{ledger_block}}", render_ledger_block(ledger))
    user = user.replace("{{exemplars_block}}", _exemplars_block(exemplars))
    user = user.replace("{{correction_block}}", _correction_block(correction))
    user = _BLANK_RUN_RE.sub("\n\n", user).strip()
    return system, user


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_draft(
    client: ModelClient,
    ledger: Ledger,
    exemplars: list[MemoryEntry],
    correction: str = "",
    stub_key: str = "",
    meta: dict | None = None,
) -> DraftResult:
    """Generate one draft at temperature 0 through the traced model client.

    Raises RuntimeError when the client exhausts its attempts; a failed
    attempt still leaves its trace line (invariant I6 lives in the client).
    The returned prompt_sha is the sha of the user prompt, which is exactly
    the prompt_sha the trace line for this call carries.
    """
    system, user = build_draft_prompt(ledger, exemplars, correction)
    resp = client.complete(
        task="draft",
        prompt=user,
        system=system,
        stub_key=stub_key,
        meta=dict(meta or {}),
        temperature=0,
    )
    if not resp.ok:
        raise RuntimeError(
            f"draft generation failed after {resp.attempts} attempts: {resp.error}"
        )
    return DraftResult(
        text=resp.text,
        system=system,
        prompt=user,
        prompt_sha=compute_prompt_sha(user),
    )
