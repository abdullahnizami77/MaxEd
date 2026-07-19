"""The revise loop, driven end to end with a programmable stub model.

The prior test suite only asserted decide() in isolation, so the revise ->
regenerate -> recheck path was never exercised: the stub always returned a
golden draft that passed at revision 0. These tests return a WRONG draft
first and a corrected draft on revision, and assert the correction the gate
produced actually reaches the second prompt and the loop converges.
"""

from __future__ import annotations

import json

import pytest

from balancecheck.config import Config
from balancecheck.contracts.models import GenerationEvent, TraceEvent, VerificationEvent
from balancecheck.drafting.golden import golden_draft
from balancecheck.model_client import ModelClient
from balancecheck.runner import run_scenario
from balancecheck.spine.events import read_events
from balancecheck.substrate import derive
from balancecheck.substrate.foundry import build_all

LEDGERS = {l.scenario_id: l for l in build_all()}


def _draft_prompts(events) -> list[str]:
    # The full revision prompt lives in the trace line, not the generation
    # event (which stores only a prompt hash).
    return [e.prompt for e in events if isinstance(e, TraceEvent) and e.task == "draft"]


def _env(tmp_path, drafts_by_rev: dict[int, str], scenario_id: str):
    """A stub whose draft depends on the revision index, so the loop can be
    driven through a wrong draft and a corrected one."""
    stubs: dict[str, dict] = {}
    for rev, text in drafts_by_rev.items():
        stubs[f"t:{scenario_id}:draft:{rev}"] = {"text": text}
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps(stubs))
    cfg = Config(mode="stub", log_path=tmp_path / "events.jsonl", stub_file=stub_file)
    return cfg


def test_completeness_revise_delivers_a_nonempty_correction_and_converges(tmp_path):
    """The credit-omission escape: a first draft that never mentions the open
    credit memo must be revised with a correction that actually names it, and
    the corrected draft must reach the human gate."""
    ledger = LEDGERS["S-A-04"]  # carries an open credit memo CM-0025
    good = golden_draft(ledger)
    without_credit = "\n".join(
        line for line in good.splitlines()
        if not any(cm.id in line for cm in ledger.credit_memos)
    )
    cfg = _env(tmp_path, {0: without_credit, 1: good}, ledger.scenario_id)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")

    events = read_events(cfg.log_path)
    gens = [e for e in events if isinstance(e, GenerationEvent)]
    vers = [e for e in events if isinstance(e, VerificationEvent)]
    # revision 0 was told to revise for missing content...
    assert vers[0].decision.action.value == "revise"
    assert vers[0].decision.reason == "draft is missing required content"
    # ...with a correction that actually names the missing credit memo.
    assert vers[0].decision.payload
    assert "CM-0025" in vers[0].decision.payload
    # the revision prompt the drafter received carried that correction.
    assert len(gens) == 2
    prompts = _draft_prompts(events)
    assert len(prompts) == 2 and "CM-0025" in prompts[1]
    # and the corrected draft converged to the human gate.
    assert outcome.terminal_action == "human_gate"
    assert outcome.revision_count == 1


def test_itemization_revise_delivers_a_correction(tmp_path):
    """A draft that lists open invoices at their full original amounts (a
    correct net beside an inconsistent itemization) is revised with a
    correction that states the open amounts, and converges when fixed."""
    ledger = LEDGERS["S-A-03"]  # INV-3063 partially paid: original 8200, open 5200
    good = golden_draft(ledger)
    from balancecheck.substrate.money import render, cents
    lines = [f"Dear {ledger.client.name} team,", "",
             f"The total balance due is {render(derive.net_balance(ledger))}.", "",
             "The following invoices are open:"]
    for inv in ledger.invoices:
        if derive.open_amount(ledger, inv.id) > 0:
            lines.append(f"- {inv.id}: {render(cents(inv.amount_cents))}")  # full amount, wrong
    lines += ["", "Accounts Receivable"]
    bad = "\n".join(lines)
    cfg = _env(tmp_path, {0: bad, 1: good}, ledger.scenario_id)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")

    vers = [e for e in read_events(cfg.log_path) if isinstance(e, VerificationEvent)]
    assert vers[0].decision.action.value == "revise"
    assert vers[0].decision.reason == "itemized amounts contradict the computed totals"
    assert vers[0].decision.payload and "$5,200.00" in vers[0].decision.payload
    assert outcome.terminal_action == "human_gate"


def test_claim_level_revise_correction_reaches_the_prompt(tmp_path):
    """A wrong amount at revision 0 produces a correction naming the right
    figure, and that correction reaches the revision prompt."""
    ledger = LEDGERS["S-A-01"]
    good = golden_draft(ledger)
    net = derive.net_balance(ledger)
    from balancecheck.substrate.money import render, cents
    wrong = good.replace(render(net), render(cents(int(net) + 100000)), 1)
    cfg = _env(tmp_path, {0: wrong, 1: good}, ledger.scenario_id)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    events = read_events(cfg.log_path)
    vers = [e for e in events if isinstance(e, VerificationEvent)]
    assert vers[0].decision.action.value == "revise"
    assert vers[0].decision.payload  # a real correction, never empty
    assert render(net) in vers[0].decision.payload
    prompts = _draft_prompts(events)
    assert len(prompts) == 2 and render(net) in prompts[1]  # correction reached the drafter
    assert outcome.terminal_action == "human_gate"


def test_oscillation_guard_escalates_when_the_model_repeats(tmp_path):
    """If the model returns the same wrong draft on every revision, the loop
    must not spin: the oscillation guard escalates."""
    ledger = LEDGERS["S-A-04"]
    good = golden_draft(ledger)
    without_credit = "\n".join(
        line for line in good.splitlines()
        if not any(cm.id in line for cm in ledger.credit_memos)
    )
    # every revision returns the same incomplete draft
    cfg = _env(tmp_path, {0: without_credit, 1: without_credit, 2: without_credit},
               ledger.scenario_id)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "escalate"
    assert outcome.reason == "revision loop"
