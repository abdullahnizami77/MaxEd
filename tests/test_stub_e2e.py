"""The end-to-end stub run: the whole loop, no model, zero credentials.

Builds fixtures in-process, writes golden-draft stubs to a temp file, runs
the full generate-verify-decide loop on a clean scenario and the ambiguous
one, and asserts the terminal states the design promises. Also carries
invariant I6 (trace coverage): every model-call attempt emitted exactly one
trace line.
"""

from __future__ import annotations

import json

import pytest

from balancecheck.config import Config
from balancecheck.contracts.models import (
    CapabilityGapEvent,
    GenerationEvent,
    ScoreEvent,
    TraceEvent,
    VerificationEvent,
)
from balancecheck.drafting.golden import golden_draft
from balancecheck.model_client import ModelClient
from balancecheck.runner import run_scenario
from balancecheck.spine.events import read_events
from balancecheck.substrate.foundry import build_all


@pytest.fixture()
def env(tmp_path):
    ledgers = {l.scenario_id: l for l in build_all()}
    stubs = {}
    for sid, ledger in ledgers.items():
        text = golden_draft(ledger)
        for rev in range(3):
            stubs[f"e2e:{sid}:draft:{rev}"] = {"text": text}
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps(stubs))
    cfg = Config(mode="stub", log_path=tmp_path / "events.jsonl", stub_file=stub_file)
    return cfg, ledgers


def test_clean_scenario_reaches_human_gate(env):
    cfg, ledgers = env
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(
        cfg, client, ledgers["S-A-01"], pass_label="t", run_id="t", stub_prefix="e2e"
    )
    assert outcome.terminal_action == "human_gate"
    assert outcome.revision_count == 0
    events = read_events(cfg.log_path)
    gens = [e for e in events if isinstance(e, GenerationEvent)]
    vers = [e for e in events if isinstance(e, VerificationEvent)]
    scores = [e for e in events if isinstance(e, ScoreEvent)]
    assert len(gens) == 1 and gens[0].is_first_pass
    assert len(vers) == 1 and vers[0].claims, "claims were extracted and recorded"
    stages = {s.stage for s in scores}
    assert stages == {"first_pass", "final"}
    first = next(s for s in scores if s.stage == "first_pass")
    assert first.grounding_total > 0
    assert first.grounding_checked == first.grounding_total, "golden draft grounds fully"


def test_ambiguous_scenario_abstains_with_gap(env):
    cfg, ledgers = env
    client = ModelClient(cfg=cfg, run_id="t")
    outcome = run_scenario(
        cfg, client, ledgers["S-A-06"], pass_label="t", run_id="t", stub_prefix="e2e"
    )
    assert outcome.terminal_action == "abstain"
    events = read_events(cfg.log_path)
    gaps = [e for e in events if isinstance(e, CapabilityGapEvent)]
    assert gaps, "an abstain emits a capability-gap record"
    assert gaps[0].missing.value == "allocation_reference"
    assert client.call_count == 0, "abstain-before-drafting spends no model calls"


def test_trace_coverage_i6(env):
    cfg, ledgers = env
    client = ModelClient(cfg=cfg, run_id="t")
    for sid in ("S-A-01", "S-A-02", "S-A-03"):
        run_scenario(
            cfg, client, ledgers[sid], pass_label="t", run_id="t", stub_prefix="e2e"
        )
    events = read_events(cfg.log_path)
    trace_lines = [e for e in events if isinstance(e, TraceEvent)]
    assert client.call_count > 0
    assert len(trace_lines) == client.call_count, (
        "invariant I6: one trace line per model-call attempt"
    )


def test_trace_fires_on_failure(tmp_path):
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text("{}")
    cfg = Config(mode="stub", log_path=tmp_path / "events.jsonl", stub_file=stub_file)
    client = ModelClient(cfg=cfg, run_id="t")
    with pytest.raises(KeyError):
        client.complete("draft", "prompt text", stub_key="missing-key")
    events = read_events(cfg.log_path)
    trace_lines = [e for e in events if isinstance(e, TraceEvent)]
    assert len(trace_lines) == 1, "a failed attempt still emits its trace line"
    assert trace_lines[0].ok is False or trace_lines[0].output == ""


def test_full_pool_terminal_states(env):
    cfg, ledgers = env
    client = ModelClient(cfg=cfg, run_id="t")
    allowed = {"human_gate", "abstain", "escalate"}
    for sid, ledger in ledgers.items():
        if ledger.pool != "A":
            continue
        outcome = run_scenario(
            cfg, client, ledger, pass_label="t", run_id="t", stub_prefix="e2e"
        )
        assert outcome.terminal_action in allowed, (
            f"{sid}: invariant I5, revise is never terminal and nothing auto-sends"
        )
