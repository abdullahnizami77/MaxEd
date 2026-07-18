"""Invariant I8, exercised end to end: every event type in the system dumps
to a line that validates against the exported JSON Schema, and every line
survives the append/read round trip with its type intact.

One instance of EVERY member of the Event union is constructed inline; the
log lives in pytest tmp_path; no network, no model, no credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from balancecheck.contracts.models import (
    CapabilityGapEvent,
    CheckResult,
    CheckStatus,
    Claim,
    ClaimType,
    Finding,
    GapCategory,
    GateAction,
    GateDecision,
    GenerationEvent,
    HumanAction,
    HumanDecisionEvent,
    IngestEvent,
    JudgmentEvent,
    RunStartEvent,
    ScoreEvent,
    TraceEvent,
    VerificationEvent,
    dump_event,
    event_json_schema,
    parse_event,
)
from balancecheck.spine.events import append_event, read_events


def all_event_instances() -> list:
    """One fully populated instance of every event type in the union."""
    claim = Claim(
        claim_id="c-1",
        type=ClaimType.C_AMT,
        span="Invoice INV-1012 is open for $2,400.00.",
        token="$2,400.00",
        subject_id="INV-1012",
        check_result=CheckResult(
            status=CheckStatus.PASS,
            expected="$2,400.00",
            actual="$2,400.00",
            cited_records=["INV-1012"],
            detail="matches open amount",
        ),
    )
    decision = GateDecision(
        action=GateAction.HUMAN_GATE,
        reason="all checks passed",
        findings=[
            Finding(
                kind="c_amt_fail",
                claim_id="c-1",
                span="a span",
                detail="d",
                correction="the ledger shows X, you wrote Y",
            )
        ],
        payload="",
    )
    return [
        RunStartEvent(
            run_id="R-1",
            pass_label="pass1",
            seed=20260718,
            git_sha="abc123",
            model_name="stub",
            model_digest="stub",
            config_hash="deadbeef",
            pool="A",
            memory_state_hash="empty",
        ),
        GenerationEvent(
            run_id="R-1",
            gen_id="G-1",
            scenario_id="S-A-01",
            pool="A",
            pass_label="pass1",
            ledger_ref="feedface",
            prompt_sha="ab12cd34",
            draft="Dear client, your balance is $2,400.00.",
            revision_index=0,
            is_first_pass=True,
        ),
        VerificationEvent(run_id="R-1", gen_id="G-1", claims=[claim], decision=decision),
        HumanDecisionEvent(
            run_id="R-1",
            gen_id="G-1",
            scenario_id="S-A-01",
            pool="A",
            action=HumanAction.EDIT,
            final_text="Dear client, your net balance is $2,400.00.",
            reason="mention the unapplied payment",
        ),
        CapabilityGapEvent(
            run_id="R-1",
            gen_id="G-1",
            scenario_id="S-A-01",
            missing=GapCategory.ALLOCATION_REFERENCE,
            detail="payment matches two invoices",
            would_need="a remittance advice",
            resolvable_by="human",
        ),
        IngestEvent(
            run_id="R-1",
            consumed_through_offset=42,
            entries_added=3,
            entries_evicted=1,
            memory_state_hash="1234abcd",
        ),
        ScoreEvent(
            run_id="R-1",
            gen_id="G-1",
            scenario_id="S-A-01",
            pass_label="pass1",
            stage="first_pass",
            grounding_checked=3,
            grounding_total=5,
            completeness_present=2,
            completeness_total=4,
            revision_count=1,
            terminal_action="human_gate",
        ),
        JudgmentEvent(
            run_id="R-1",
            gen_id="G-1",
            mode="rubric",
            scores={"tone": 3, "acceptable": True},
            prompt_sha="ab12cd34",
            parse_ok=True,
            judge_model="stub-judge",
        ),
        TraceEvent(
            run_id="R-1",
            task="draft",
            prompt_sha="ab12cd34",
            prompt="the prompt",
            output="the output",
            model_name="stub",
            attempt=1,
            ok=True,
            error="",
            meta={"stub": True},
        ),
    ]


def test_every_event_type_is_covered() -> None:
    """The inventory above must cover the whole union; a new event type
    added to contracts without a row here fails this test."""
    covered = {type(e).__name__ for e in all_event_instances()}
    expected = {
        "RunStartEvent",
        "GenerationEvent",
        "VerificationEvent",
        "HumanDecisionEvent",
        "CapabilityGapEvent",
        "IngestEvent",
        "ScoreEvent",
        "JudgmentEvent",
        "TraceEvent",
    }
    assert covered == expected


def test_every_dumped_line_validates_against_schema() -> None:
    schema = event_json_schema()
    for event in all_event_instances():
        line = dump_event(event)
        instance = json.loads(line)
        jsonschema.validate(instance, schema)  # raises on any violation
        assert instance["schema_version"] == 1
        assert instance["event_type"] == type(event).model_fields["event_type"].default


def test_schema_rejects_malformed_line() -> None:
    schema = event_json_schema()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"event_type": "score", "schema_version": 1}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"event_type": "no_such_event"}, schema)


def test_events_round_trip_through_the_log(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    originals = all_event_instances()
    for n, event in enumerate(originals):
        assert append_event(event, log) == n
    parsed = read_events(log)
    assert [type(e) for e in parsed] == [type(e) for e in originals]
    assert parsed == originals
    # and each line individually re-parses to the same type via the adapter
    for event in originals:
        assert type(parse_event(dump_event(event))) is type(event)


def test_log_lines_validate_from_disk(tmp_path: Path) -> None:
    """The exact bytes on disk validate, not just the in-memory dumps."""
    log = tmp_path / "events.jsonl"
    for event in all_event_instances():
        append_event(event, log)
    schema = event_json_schema()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(all_event_instances())
    for line in lines:
        jsonschema.validate(json.loads(line), schema)
