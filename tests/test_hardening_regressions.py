"""Regression tests for the peripheral adversarial-review fixes.

Each test pins a fix from the execution-based review: strict money parsing,
trace-line honesty under retry, the torn-log-line guard, crash-safe ingest,
the oracle's zero-cent blind spot, and the report marker guards.
"""

from __future__ import annotations

import json

import pytest

from balancecheck.config import Config
from balancecheck.contracts.models import (
    HumanAction,
    HumanDecisionEvent,
    ScoreEvent,
    TraceEvent,
)
from balancecheck.memory.ingest import ingest
from balancecheck.memory.store import MemoryStore
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import append_event, read_events
from balancecheck.spine.report import before_after_table
from balancecheck.substrate.foundry import build_all
from balancecheck.substrate.money import parse_dollars


# ---------------------------------------------------------------------------
# money: strict parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("garbage", [",", ",,,", "$,", "$1,23,456", "$6,30.00", "1,2", "$"])
def test_parse_dollars_rejects_garbage_and_bad_grouping(garbage):
    with pytest.raises(ValueError):
        parse_dollars(garbage)


@pytest.mark.parametrize(
    ("text", "want"),
    [("$1,234,567.89", 123456789), ("$999", 99900), ("1234.5", 123450), ("9,999", 999900)],
)
def test_parse_dollars_still_accepts_wellformed(text, want):
    assert parse_dollars(text) == want


# ---------------------------------------------------------------------------
# model client: the trace records the prompt actually sent per attempt
# ---------------------------------------------------------------------------


def test_trace_records_the_sent_prompt_not_the_retry_prompt(tmp_path):
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps({"k": {"text": "this is not json"}}))
    cfg = Config(mode="stub", log_path=tmp_path / "ev.jsonl", stub_file=stub_file)
    client = ModelClient(cfg=cfg)
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    resp = client.complete("judge_rubric", "ORIGINAL PROMPT", schema=schema, stub_key="k")
    assert not resp.ok
    traces = [e for e in read_events(cfg.log_path) if isinstance(e, TraceEvent)]
    assert len(traces) == cfg.max_attempts
    assert traces[0].prompt == "ORIGINAL PROMPT", (
        "attempt 1 must log the prompt it sent, not attempt 2's retry prompt"
    )
    assert "previous output was not valid" in traces[1].prompt, (
        "attempt 2 logs the retry prompt it actually sent"
    )


def test_stub_key_missing_failure_has_a_reason(tmp_path):
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text("{}")
    cfg = Config(mode="stub", log_path=tmp_path / "ev.jsonl", stub_file=stub_file)
    client = ModelClient(cfg=cfg)
    with pytest.raises(KeyError):
        client.complete("draft", "p", stub_key="missing")
    traces = [e for e in read_events(cfg.log_path) if isinstance(e, TraceEvent)]
    assert len(traces) == 1
    assert "missing" in traces[0].error and traces[0].error != ""


# ---------------------------------------------------------------------------
# events: torn-tail guard
# ---------------------------------------------------------------------------


def test_append_after_torn_line_preserves_the_new_event(tmp_path):
    log = tmp_path / "ev.jsonl"
    append_event(TraceEvent(task="a", prompt_sha="x", prompt="p", output="o",
                            model_name="stub", attempt=1, ok=True), log)
    # Simulate a crash mid-write: a truncated fragment with no newline.
    with log.open("a", encoding="utf-8") as f:
        f.write('{"event_type": "trace", "task": "torn')
    append_event(TraceEvent(task="b", prompt_sha="y", prompt="p2", output="o2",
                            model_name="stub", attempt=1, ok=True), log)
    lines = log.read_text().splitlines()
    assert len(lines) == 3, "stump sealed into its own line, new event intact"
    last = json.loads(lines[-1])
    assert last["task"] == "b", "the new event parses cleanly from the last line"


# ---------------------------------------------------------------------------
# ingest: crash safety and fixture-pool authority
# ---------------------------------------------------------------------------


def _decision(gen_id, scenario_id, pool="A"):
    return HumanDecisionEvent(
        gen_id=gen_id,
        scenario_id=scenario_id,
        pool=pool,
        action=HumanAction.APPROVE,
        final_text="approved text",
        reason="fine",
    )


def _write_fixture(fixtures_dir, ledger):
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / f"{ledger.scenario_id}.json").write_text(ledger.model_dump_json(indent=2))


def test_crash_mid_ingest_adds_nothing_and_rerun_is_exact(tmp_path):
    ledgers = build_all()
    a1 = ledgers[0]  # S-A-01
    a2 = ledgers[1]  # S-A-02
    fixtures = tmp_path / "fixtures"
    _write_fixture(fixtures, a1)  # a2's fixture is deliberately missing
    log = tmp_path / "ev.jsonl"
    append_event(_decision("g1", a1.scenario_id), log)
    append_event(_decision("g2", a2.scenario_id), log)
    store = MemoryStore(tmp_path / "mem.json")
    meta = tmp_path / "meta.json"
    cfg = Config(mode="stub", log_path=log, stub_file=tmp_path / "s.json")
    with pytest.raises(FileNotFoundError):
        ingest(log, store, meta, fixtures, cfg)
    assert store.entries() == [], "a crash mid-batch adds nothing (two-phase)"
    _write_fixture(fixtures, a2)
    result = ingest(log, store, meta, fixtures, cfg)
    assert result["added"] == 2
    assert len(store.entries()) == 2, "exactly one entry per decision after the rerun"


def test_fixture_pool_is_the_authority_over_the_event_pool(tmp_path):
    ledgers = build_all()
    b1 = next(l for l in ledgers if l.pool == "B")
    fixtures = tmp_path / "fixtures"
    _write_fixture(fixtures, b1)
    log = tmp_path / "ev.jsonl"
    # The event lies: it claims pool A for a scenario whose fixture is pool B.
    append_event(_decision("g1", b1.scenario_id, pool="A"), log)
    store = MemoryStore(tmp_path / "mem.json")
    cfg = Config(mode="stub", log_path=log, stub_file=tmp_path / "s.json")
    result = ingest(log, store, tmp_path / "meta.json", fixtures, cfg)
    assert result["refused_pool_b"] == 1
    assert store.entries() == [], "invariant I7 holds against a lying event"


# ---------------------------------------------------------------------------
# report: unpaired scenarios are named, never silently dropped
# ---------------------------------------------------------------------------


def _score(pass_label, scenario_id):
    return ScoreEvent(
        gen_id=f"{pass_label}-{scenario_id}-r0",
        scenario_id=scenario_id,
        pass_label=pass_label,
        stage="first_pass",
        grounding_checked=3,
        grounding_total=4,
        completeness_present=2,
        completeness_total=2,
        revision_count=0,
        terminal_action="human_gate",
    )


def test_before_after_names_unpaired_scenarios():
    events = [_score("pass1", "S-B-01"), _score("pass2", "S-B-01"), _score("pass1", "S-B-02")]
    table = before_after_table(events)
    assert "unpaired: S-B-02 (pass1 only)" in table
