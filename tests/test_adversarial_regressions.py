"""The adversarial red-team corpus, locked in as regression tests.

An execution-based red team walked 18 of 23 crafted wrong drafts through the
original verifier to the human gate. After hardening (context-strict
no-subject amounts, separator-tolerant document IDs, status paraphrases,
marker-free money shapes, abbreviation-aware sentence splitting), every
materially wrong sentence below must terminate in revise, escalate, or
abstain, and every true sentence must still reach the human gate. This file
is the proof the teeth stay sharp.
"""

from __future__ import annotations

import pytest

from balancecheck.checks.checks import run_code_checks
from balancecheck.drafting.golden import golden_draft
from balancecheck.drafting.surface import extract_claims
from balancecheck.gate.policy import decide
from balancecheck.substrate.foundry import build_all

CAUGHT = {"revise", "escalate", "abstain"}


@pytest.fixture(scope="module")
def ledger():
    # Ground truth for S-A-02: net owed $2,150.00; open-invoice total
    # $8,450.00; INV-2987 overdue with $6,300.00 open; INV-3014 open for
    # $2,150.00; payments on file: $9,875.50 (applied) and $6,300.00
    # (unapplied).
    return {l.scenario_id: l for l in build_all()}["S-A-02"]


def _decide(ledger, sentence: str) -> str:
    claims = extract_claims(sentence, ledger)
    run_code_checks(claims, ledger)
    return decide(claims, ledger, revision_index=0, draft_hashes=[]).action.value


WRONG_SENTENCES = [
    # Wrong payment requests: only the net balance is a defensible ask.
    "Please remit $6,300.00 at your earliest convenience.",
    "Please remit $8,450.00 to bring the account current.",
    "Kindly send us $9,875.50 to close things out.",
    "Please send USD 6300 now.",
    "Kindly wire $ 6,300 today.",
    "Remit sixty-three hundred dollars.",
    "Amount to remit is $6,300-.",
    # Wrong received assertions: no such payment exists.
    "We received $2,150.00 from you.",
    # Fabricated documents in non-canonical forms.
    "Your inv 9999 remains overdue and must be paid.",
    "Please see Invoice No. 9999 attached.",
    "Reference INV_9999 for the balance.",
    # The split-sentence wrong-total bypass.
    "You currently owe us. The figure stands at $8,450.00.",
    # Paid-paraphrases on an overdue invoice.
    "INV-2987 has been taken care of.",
    "Nothing further is owed on INV-2987.",
    # Paid-paraphrase with no document reference: goes to the fuzzy tier.
    "That invoice is settled in full.",
    # Marker-free money shapes.
    "The remaining amount is 9,999.",
    "The total comes to 6,300.00 (USD).",
    # Full amount asserted as open on a specific invoice.
    "INV-3014 is open for $6,300.00.",
    # Wrong status through the normal vocabulary.
    "INV-3014 is settled.",
    # The classic ignores-unapplied misreading.
    "Your total balance due is $8,450.00.",
]

TRUE_SENTENCES = [
    "Please remit $2,150.00 at your convenience.",
    "We received $6,300.00 from you last month.",
    "A balance of (2,150.00) remains.",
    "The two open invoices total $8,450.00.",
]


@pytest.mark.parametrize("sentence", WRONG_SENTENCES)
def test_wrong_sentence_is_caught(ledger, sentence):
    action = _decide(ledger, sentence)
    assert action in CAUGHT, f"escaped to {action}: {sentence!r}"


@pytest.mark.parametrize("sentence", TRUE_SENTENCES)
def test_true_sentence_is_not_condemned(ledger, sentence):
    action = _decide(ledger, sentence)
    assert action == "human_gate", f"true statement flagged as {action}: {sentence!r}"


def test_golden_drafts_survive_the_hardening():
    """Strictness must never reject a correct draft: every golden draft still
    grounds fully and terminates at the human gate (or abstains on the
    designed ambiguity)."""
    for ledger in build_all():
        claims = extract_claims(golden_draft(ledger), ledger)
        run_code_checks(claims, ledger)
        decision = decide(claims, ledger, revision_index=0, draft_hashes=[])
        expected = (
            "abstain" if ledger.scenario_id.endswith("-06") else "human_gate"
        )
        assert decision.action.value == expected, (
            f"{ledger.scenario_id}: {decision.action.value} ({decision.reason})"
        )
