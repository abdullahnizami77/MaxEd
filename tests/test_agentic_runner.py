"""Runner integration for the agentic recovery path.

The whole loop is driven in stub mode: draft stubs feed the normal drafter
and JSON step stubs feed the agent (the orchestrator's stub keys are
deterministic), so these tests exercise the real runner, real extractor,
real checks, and real gate with zero network. The env-driven config guard
forbids agentic+stub for users; tests construct Config directly on purpose.
"""

from __future__ import annotations

import json

import pytest

from balancecheck.config import Config
from balancecheck.contracts.models import (
    GenerationEvent,
    ToolCallEvent,
    TraceEvent,
    VerificationEvent,
)
from balancecheck.drafting.golden import golden_draft
from balancecheck.model_client import ModelClient
from balancecheck.runner import AGENTIC_EXHAUSTED, run_scenario
from balancecheck.spine.events import read_events
from balancecheck.substrate.foundry import build_all

LEDGERS = {l.scenario_id: l for l in build_all()}


def _step(kind: str, tool_calls=None, draft: str = "", reason: str = "note") -> str:
    return json.dumps(
        {"kind": kind, "reason": reason, "tool_calls": tool_calls or [], "draft": draft}
    )


def _cfg(tmp_path, stubs: dict, revision_mode: str = "agentic") -> Config:
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps({k: {"text": v} for k, v in stubs.items()}))
    return Config(
        mode="stub",
        log_path=tmp_path / "events.jsonl",
        stub_file=stub_file,
        revision_mode=revision_mode,
    )


def _stripped_credit_draft(ledger) -> str:
    good = golden_draft(ledger)
    return "\n".join(
        line for line in good.splitlines()
        if not any(cm.id in line for cm in ledger.credit_memos)
    )


COVERAGE_CALLS = [
    {"name": "get_account_summary", "arguments": {}},
    {"name": "list_open_invoices", "arguments": {}},
    {"name": "list_unapplied_sources", "arguments": {}},
]


def test_clean_r0_never_activates_the_agent(tmp_path):
    """Acceptance 2: a clean first draft uses one LLM call and zero tools,
    even in agentic mode."""
    ledger = LEDGERS["S-A-01"]
    cfg = _cfg(tmp_path, {f"t:{ledger.scenario_id}:draft:0": golden_draft(ledger)})
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "human_gate"
    assert client.call_count == 1
    events = read_events(cfg.log_path)
    assert not [e for e in events if isinstance(e, ToolCallEvent)]
    gens = [e for e in events if isinstance(e, GenerationEvent)]
    assert [g.generation_mode for g in gens] == ["prompt_initial"]


def test_r0_revise_activates_agent_once_and_recovers(tmp_path):
    """Acceptance 3/6: a correctable r0 failure activates exactly one bounded
    recovery, and the agent's draft is independently re-verified to the
    human gate."""
    ledger = LEDGERS["S-A-04"]  # open credit memo: its omission is correctable
    bad = _stripped_credit_draft(ledger)
    good = golden_draft(ledger)
    stubs = {
        f"t:{ledger.scenario_id}:draft:0": bad,
        f"t:{ledger.scenario_id}:agent:0": _step("tool_calls", COVERAGE_CALLS),
        f"t:{ledger.scenario_id}:agent:1": _step("final", draft=good),
    }
    cfg = _cfg(tmp_path, stubs)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "human_gate"
    assert outcome.revision_count == 1

    events = read_events(cfg.log_path)
    gens = [e for e in events if isinstance(e, GenerationEvent)]
    assert [g.generation_mode for g in gens] == ["prompt_initial", "agentic_recovery"]
    tools = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tools) == 3 and all(t.ok for t in tools)
    # the agent draft went through the real verifier: its verification event
    # carries extracted claims, and the terminal decision is the gate's.
    vers = [e for e in events if isinstance(e, VerificationEvent)]
    assert vers[-1].claims, "agent draft was re-verified claim by claim"
    assert vers[-1].decision.action.value == "human_gate"
    # budget: 1 draft call + 2 agent steps
    traces = [e for e in events if isinstance(e, TraceEvent)]
    assert len(traces) == client.call_count == 3


def test_failed_agentic_recovery_escalates_not_loops(tmp_path):
    """Acceptance 7: a recovery that still fails terminates as ESCALATE with
    the agentic_recovery_exhausted reason; no autonomous loop."""
    ledger = LEDGERS["S-A-04"]
    bad = _stripped_credit_draft(ledger)
    # A DIFFERENT draft that still omits the credit memo: the exhaustion
    # conversion fires (an identical repeat would trip the oscillation guard
    # first, which is pinned separately below).
    still_bad = bad.replace("Dear", "Hello")
    stubs = {
        f"t:{ledger.scenario_id}:draft:0": bad,
        f"t:{ledger.scenario_id}:agent:0": _step("tool_calls", COVERAGE_CALLS),
        f"t:{ledger.scenario_id}:agent:1": _step("final", draft=still_bad),
    }
    cfg = _cfg(tmp_path, stubs)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "escalate"
    assert outcome.reason == AGENTIC_EXHAUSTED
    gens = [e for e in read_events(cfg.log_path) if isinstance(e, GenerationEvent)]
    assert len(gens) == 2, "exactly one recovery, never a second autonomous attempt"


def test_agent_repeating_the_identical_draft_trips_the_oscillation_guard(tmp_path):
    """An agent that hands back the byte-identical failed draft is cycling;
    the oscillation guard escalates with its own reason."""
    ledger = LEDGERS["S-A-04"]
    bad = _stripped_credit_draft(ledger)
    stubs = {
        f"t:{ledger.scenario_id}:draft:0": bad,
        f"t:{ledger.scenario_id}:agent:0": _step("tool_calls", COVERAGE_CALLS),
        f"t:{ledger.scenario_id}:agent:1": _step("final", draft=bad),
    }
    cfg = _cfg(tmp_path, stubs)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "escalate"
    assert outcome.reason == "revision loop"


def test_agent_producing_no_draft_escalates(tmp_path):
    """An agent that never finalizes within its budgets yields no draft; the
    scenario escalates instead of retrying."""
    ledger = LEDGERS["S-A-04"]
    bad = _stripped_credit_draft(ledger)
    never_final = _step("tool_calls", [{"name": "get_account_summary", "arguments": {}}])
    stubs = {
        f"t:{ledger.scenario_id}:draft:0": bad,
        f"t:{ledger.scenario_id}:agent:0": never_final,
        f"t:{ledger.scenario_id}:agent:1": never_final,
        f"t:{ledger.scenario_id}:agent:2": never_final,
    }
    cfg = _cfg(tmp_path, stubs)
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "escalate"
    assert outcome.reason == AGENTIC_EXHAUSTED
    assert outcome.final_draft == ""


def test_prompt_mode_default_is_unchanged(tmp_path):
    """Acceptance 1: with the default configuration the loop behaves exactly
    as before, prompt revisions and all."""
    ledger = LEDGERS["S-A-04"]
    bad = _stripped_credit_draft(ledger)
    good = golden_draft(ledger)
    stubs = {
        f"t:{ledger.scenario_id}:draft:0": bad,
        f"t:{ledger.scenario_id}:draft:1": good,
    }
    cfg = _cfg(tmp_path, stubs, revision_mode="prompt")
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="t")
    assert outcome.terminal_action == "human_gate"
    events = read_events(cfg.log_path)
    gens = [e for e in events if isinstance(e, GenerationEvent)]
    assert [g.generation_mode for g in gens] == ["prompt_initial", "prompt_revision"]
    assert not [e for e in events if isinstance(e, ToolCallEvent)]


def test_no_send_action_exists():
    """The gate's action vocabulary contains no send/approve verb; the only
    successful terminal is the human gate."""
    from balancecheck.contracts.models import GateAction

    assert {a.value for a in GateAction} == {"revise", "abstain", "escalate", "human_gate"}
