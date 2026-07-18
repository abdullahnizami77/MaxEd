"""Ingest is idempotent by consumed-offset (plan section 13).

Calling ingest twice over the same log adds nothing the second time, the
recorded offset advances only past its own bookkeeping line (never
reprocessing a decision), and every call appends exactly one IngestEvent.
Hermetic: inline ledger, tmp_path for the log, store, meta, and fixtures.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from balancecheck.config import Config
from balancecheck.contracts.models import (
    ClientInfo,
    HumanAction,
    HumanDecisionEvent,
    IngestEvent,
    Invoice,
    Ledger,
    Payment,
    TraceEvent,
)
from balancecheck.memory.ingest import ingest, read_consumed_offset
from balancecheck.memory.store import MemoryStore
from balancecheck.spine.events import append_event, line_count, read_events


def make_ledger(scenario_id: str) -> Ledger:
    return Ledger(
        scenario_id=scenario_id,
        pool="A",
        client=ClientInfo(id="CL-T", name="Test Client", terms_days=30, as_of=date(2026, 3, 31)),
        invoices=[
            Invoice(id="INV-1001", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=240000)
        ],
        payments=[Payment(id="PMT-0100", date=date(2026, 2, 15), amount_cents=50000)],
        credit_memos=[],
        applications=[],
    )


def decision(gen_id: str, action: HumanAction) -> HumanDecisionEvent:
    return HumanDecisionEvent(
        gen_id=gen_id,
        scenario_id="S-A-77",
        pool="A",
        action=action,
        final_text="Dear client, your balance is $1,900.00.",
        reason="test decision",
    )


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    ledger = make_ledger("S-A-77")
    (fixtures / "S-A-77.json").write_text(ledger.model_dump_json(indent=2) + "\n", encoding="utf-8")
    log = tmp_path / "events.jsonl"
    # A non-decision line first, so ingest demonstrably skips other event types.
    append_event(
        TraceEvent(
            task="draft",
            prompt_sha="abc",
            prompt="p",
            output="o",
            model_name="stub",
            attempt=1,
            ok=True,
        ),
        log,
    )
    append_event(decision("g-1", HumanAction.APPROVE), log)
    append_event(decision("g-2", HumanAction.EDIT), log)
    return {
        "log": log,
        "store": MemoryStore(tmp_path / "memory.json"),
        "meta": tmp_path / "ingest_meta.json",
        "fixtures": fixtures,
        "cfg": Config(log_path=log, stub_file=tmp_path / "stub.json"),
    }


def ingest_events(log: Path) -> list[IngestEvent]:
    return [e for e in read_events(log) if isinstance(e, IngestEvent)]


def test_second_ingest_adds_nothing(env: dict) -> None:
    first = ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    assert first["added"] == 2
    assert first["refused_pool_b"] == 0
    assert len(env["store"].entries()) == 2

    second = ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    assert second == {"added": 0, "evicted": 0, "refused_pool_b": 0, "skipped_decline": 0}
    entries = env["store"].entries()
    assert len(entries) == 2, "second ingest must not duplicate entries"
    assert len({e.entry_id for e in entries}) == 2


def test_offset_advances_past_decisions_once(env: dict) -> None:
    ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    # Three pre-existing lines were consumed; the offset covers exactly them.
    assert read_consumed_offset(env["meta"]) == 3
    assert line_count(env["log"]) == 4  # plus the IngestEvent bookkeeping line

    ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    # The second call advances only past the first call's bookkeeping line.
    assert read_consumed_offset(env["meta"]) == 4
    assert line_count(env["log"]) == 5


def test_exactly_one_ingest_event_per_call(env: dict) -> None:
    ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    events = ingest_events(env["log"])
    assert len(events) == 1
    assert events[0].entries_added == 2
    assert events[0].entries_evicted == 0
    assert events[0].consumed_through_offset == 3
    assert events[0].memory_state_hash == env["store"].state_hash()
    assert events[0].memory_state_hash != "empty"

    ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    events = ingest_events(env["log"])
    assert len(events) == 2
    assert events[1].entries_added == 0
    assert events[1].consumed_through_offset == 4
    # Nothing was added, so the memory state hash is unchanged.
    assert events[1].memory_state_hash == events[0].memory_state_hash


def test_decline_is_counted_but_not_consumed(env: dict) -> None:
    append_event(decision("g-3", HumanAction.DECLINE), env["log"])
    result = ingest(env["log"], env["store"], env["meta"], env["fixtures"], env["cfg"])
    assert result["added"] == 2
    assert result["skipped_decline"] == 1
    assert {e.source_gen_id for e in env["store"].entries()} == {"g-1", "g-2"}
