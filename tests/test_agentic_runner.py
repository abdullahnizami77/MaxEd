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


# ---------------------------------------------------------------------------
# Regression tests for the four review findings on the orchestrator
# ---------------------------------------------------------------------------

import tempfile as _tempfile  # noqa: E402
import pathlib as _pathlib  # noqa: E402
from dataclasses import dataclass as _dataclass  # noqa: E402

from balancecheck.contracts.models import ToolCallEvent as _ToolCallEvent  # noqa: E402
from balancecheck.drafting.agentic import generate_agentic_revision  # noqa: E402


@_dataclass
class _Resp:
    ok: bool
    parsed: dict
    text: str = ""


class _Scripted:
    def __init__(self, steps):
        self.steps = steps
        self.i = 0

    def complete(self, **kw):
        s = self.steps[self.i]
        self.i += 1
        return _Resp(True, s, json.dumps(s))


def _astep(kind, calls=None, draft=""):
    return {"kind": kind, "reason": "n", "tool_calls": calls or [], "draft": draft}


def _acfg(tmp_path, **kw):
    return Config(
        mode="stub",
        log_path=tmp_path / "e.jsonl",
        stub_file=tmp_path / "s.json",
        revision_mode="agentic",
        **kw,
    )


_S03 = LEDGERS["S-A-03"]
_GOOD_03 = (
    "Dear Harbor & Finch Design Co. team,\n\n"
    "The balance due on your account is $6,650.50.\n\n"
    "* INV-3063, issued 2026-01-15, due 2026-02-14: $5,200.00 open.\n"
    "* INV-3079, issued 2026-02-26, due 2026-03-28: $1,450.50 open.\n\n"
    "Please reply if any of these figures do not match your records.\n\n"
    "Kind regards,\nAccounts Receivable"
)


def test_b1_forced_document_call_uses_the_real_tool_argument_name(tmp_path):
    """The forced coverage call for a failed invoice must succeed against the
    real execute_tool (its schema requires invoice_id, not id)."""
    steps = [
        _astep("tool_calls", [{"name": "get_account_summary", "arguments": {}},
                              {"name": "list_open_invoices", "arguments": {}}]),
        _astep("tool_calls", [{"name": "get_account_summary", "arguments": {}}]),
        _astep("final", draft=_GOOD_03),
    ]
    cfg = _acfg(tmp_path)
    ar = generate_agentic_revision(
        client=_Scripted(steps), cfg=cfg, ledger=_S03, previous_draft="bad",
        correction="c", decision_reason="c_amt_fail", failed_subject_ids=["INV-3063"],
        exemplars=[], gen_id="g", revision_index=1, run_id="r",
    )  # default executor = the REAL execute_tool
    forced = [e for e in read_events(cfg.log_path)
              if isinstance(e, _ToolCallEvent) and e.round_index == -1]
    invoice_forced = [e for e in forced if e.tool_name == "get_invoice"]
    assert invoice_forced and all(e.ok for e in invoice_forced), "forced get_invoice must succeed"
    assert ar.text  # coverage met, draft accepted


def test_b2_unfetchable_required_coverage_fails_the_recovery(tmp_path):
    """If required evidence cannot be gathered (a document the tools cannot
    return), the final is NOT accepted: the recovery fails and escalates."""
    steps = [
        _astep("tool_calls", [{"name": "get_account_summary", "arguments": {}}]),
        _astep("tool_calls", [{"name": "get_account_summary", "arguments": {}}]),
        _astep("final", draft=_GOOD_03),
    ]
    cfg = _acfg(tmp_path)
    ar = generate_agentic_revision(
        client=_Scripted(steps), cfg=cfg, ledger=_S03, previous_draft="bad",
        correction="c", decision_reason="c_exist_fail", failed_subject_ids=["INV-9999"],
        exemplars=[], gen_id="g", revision_index=1, run_id="r",
    )
    assert ar.text == "", "an ungrounded final must not be accepted"


def test_b3_total_executions_never_exceed_the_ceiling(tmp_path):
    """The total-tool ceiling binds forced coverage too: executions never
    exceed agentic_max_total_tool_calls even when forcing is needed."""
    r1 = [{"name": "get_source", "arguments": {"source_id": "PMT-0278"}},
          {"name": "get_source", "arguments": {"source_id": "PMT-0283"}},
          {"name": "get_application_history", "arguments": {}}]
    r2 = [{"name": "get_invoice", "arguments": {"invoice_id": "INV-3079"}},
          {"name": "get_invoice", "arguments": {"invoice_id": "INV-3106"}},
          {"name": "list_open_invoices", "arguments": {}}]
    steps = [_astep("tool_calls", r1), _astep("tool_calls", r2), _astep("final", draft=_GOOD_03)]
    cfg = _acfg(tmp_path, agentic_max_total_tool_calls=6)
    ar = generate_agentic_revision(
        client=_Scripted(steps), cfg=cfg, ledger=_S03, previous_draft="bad",
        correction="c", decision_reason="x", failed_subject_ids=["INV-3063"],
        exemplars=[], gen_id="g", revision_index=1, run_id="r",
    )
    assert ar.tool_calls_executed <= 6


def test_b4_forced_final_can_have_zero_coverage_forced(tmp_path):
    """forced_final and coverage_forced are distinct: a model that gathered
    all evidence but never emitted a final within its rounds has forced_final
    True and coverage_forced empty (the report must not conflate them)."""
    steps = [
        _astep("tool_calls", [{"name": "get_account_summary", "arguments": {}},
                              {"name": "list_open_invoices", "arguments": {}}]),
        _astep("tool_calls", [{"name": "get_application_history", "arguments": {}}]),
        _astep("final", draft=_GOOD_03),
    ]
    cfg = _acfg(tmp_path)
    ar = generate_agentic_revision(
        client=_Scripted(steps), cfg=cfg, ledger=_S03, previous_draft="bad",
        correction="c", decision_reason="itemization", failed_subject_ids=[],
        exemplars=[], gen_id="g", revision_index=1, run_id="r",
    )
    assert ar.forced_final is True
    assert ar.coverage_forced == []
    assert ar.text
