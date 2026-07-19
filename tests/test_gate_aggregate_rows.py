"""The two aggregate gate rows added after the first live error run.

Both catch drafts made of individually true claims that mislead as a whole:
an itemization contradicting the computed totals, and required content that
is simply absent. Claim-level checks cannot see either; the gate can.
"""

from __future__ import annotations

from balancecheck.checks.checks import run_code_checks
from balancecheck.drafting.golden import golden_draft
from balancecheck.drafting.surface import extract_claims
from balancecheck.gate.policy import decide
from balancecheck.substrate.foundry import build_all


def _ledgers():
    return {l.scenario_id: l for l in build_all()}


def _decide(ledger, draft, revision_index=0):
    claims = extract_claims(draft, ledger)
    run_code_checks(claims, ledger)
    return decide(
        claims, ledger, revision_index=revision_index, draft_hashes=[], draft=draft
    )


def test_full_amounts_itemization_is_caught():
    """The live T2-FULL-ORIGINAL-AMOUNTS escape: every open invoice listed at
    its full original amount, correct net stated, no unapplied sources. The
    itemized sum contradicts the open-invoice total; the gate revises."""
    ledger = _ledgers()["S-A-03"]
    lines = ["Dear team,", "", "The total balance due is $6,650.50.", ""]
    for inv in ledger.invoices:
        from balancecheck.substrate import derive
        if derive.open_amount(ledger, inv.id) > 0:
            from balancecheck.substrate.money import render, cents
            lines.append(f"- {inv.id}: {render(cents(inv.amount_cents))}")
    lines += ["", "Accounts Receivable"]
    decision = _decide(ledger, "\n".join(lines))
    assert decision.action.value == "revise"
    assert decision.reason == "itemized amounts contradict the computed totals"
    assert any(f.kind == "itemization_inconsistent" for f in decision.findings)


def test_omitted_credit_memo_is_caught():
    """The live T2-CREDITS-ASSUMED-APPLIED escape: correct net, open credit
    memo never mentioned. The completeness row revises."""
    ledger = _ledgers()["S-A-04"]
    draft = golden_draft(ledger)
    # Strip every line mentioning the credit memo.
    stripped = "\n".join(
        line
        for line in draft.splitlines()
        if not any(cm.id in line for cm in ledger.credit_memos)
    )
    decision = _decide(ledger, stripped)
    assert decision.action.value == "revise"
    assert decision.reason == "draft is missing required content"
    assert any(f.kind == "missing_open_credit_mentioned" for f in decision.findings)


def test_golden_drafts_pass_both_rows():
    for ledger in build_all():
        decision = _decide(ledger, golden_draft(ledger))
        expected = "abstain" if ledger.scenario_id.endswith("-06") else "human_gate"
        assert decision.action.value == expected, (
            f"{ledger.scenario_id}: {decision.action.value} ({decision.reason})"
        )


def test_aggregate_rows_escalate_at_budget():
    ledger = _ledgers()["S-A-04"]
    draft = golden_draft(ledger)
    stripped = "\n".join(
        line
        for line in draft.splitlines()
        if not any(cm.id in line for cm in ledger.credit_memos)
    )
    decision = _decide(ledger, stripped, revision_index=2)
    assert decision.action.value == "escalate"
