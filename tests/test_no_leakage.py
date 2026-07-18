"""Invariant I7: evaluation scenarios (Pool B) never enter memory.

Two independent layers are exercised: ingest refuses non-Pool-A decisions
before an entry is even built, and MemoryStore.add raises on a Pool B entry
constructed around Pydantic validation. Hermetic: inline ledgers, tmp_path
for the log, the store, the meta file, and the fixtures directory.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from balancecheck.config import Config
from balancecheck.contracts.models import (
    ClientInfo,
    HumanAction,
    HumanDecisionEvent,
    Invoice,
    Ledger,
    MemoryEntry,
    Payment,
)
from balancecheck.memory.ingest import ingest
from balancecheck.memory.store import MemoryStore
from balancecheck.spine.events import append_event


def make_ledger(scenario_id: str, pool: str) -> Ledger:
    """A small ledger with received-but-unapplied cash."""
    return Ledger(
        scenario_id=scenario_id,
        pool=pool,  # type: ignore[arg-type]
        client=ClientInfo(id="CL-T", name="Test Client", terms_days=30, as_of=date(2026, 3, 31)),
        invoices=[
            Invoice(id="INV-1001", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=240000)
        ],
        payments=[Payment(id="PMT-0100", date=date(2026, 2, 15), amount_cents=50000)],
        credit_memos=[],
        applications=[],
    )


def write_fixture(fixtures_dir: Path, ledger: Ledger) -> None:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    path = fixtures_dir / f"{ledger.scenario_id}.json"
    path.write_text(ledger.model_dump_json(indent=2) + "\n", encoding="utf-8")


def decision(
    scenario_id: str, pool: str, action: HumanAction, gen_id: str = "g-1"
) -> HumanDecisionEvent:
    return HumanDecisionEvent(
        gen_id=gen_id,
        scenario_id=scenario_id,
        pool=pool,
        action=action,
        final_text="Dear client, your balance is $1,900.00.",
        reason="test decision",
    )


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "log": tmp_path / "events.jsonl",
        "store": tmp_path / "memory.json",
        "meta": tmp_path / "ingest_meta.json",
        "fixtures": tmp_path / "fixtures",
    }


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    return Config(log_path=tmp_path / "events.jsonl", stub_file=tmp_path / "stub.json")


def test_pool_b_decision_is_refused_by_ingest(paths: dict[str, Path], cfg: Config) -> None:
    write_fixture(paths["fixtures"], make_ledger("S-B-77", "B"))
    append_event(decision("S-B-77", "B", HumanAction.APPROVE), paths["log"])
    store = MemoryStore(paths["store"])

    result = ingest(paths["log"], store, paths["meta"], paths["fixtures"], cfg)

    assert result["added"] == 0
    assert result["refused_pool_b"] == 1
    assert result["evicted"] == 0
    assert store.entries() == []
    assert store.state_hash() == "empty"


def test_store_add_raises_on_forced_pool_b_entry(paths: dict[str, Path]) -> None:
    store = MemoryStore(paths["store"])
    # The contract itself rejects pool B at construction time.
    with pytest.raises(ValidationError):
        MemoryEntry(
            entry_id="mem-bad",
            source_gen_id="g-9",
            scenario_id="S-B-77",
            pool="B",  # type: ignore[arg-type]
            signature={"has_unapplied_cash": True},
            weight=float(1),
            final_text="text",
            human_action=HumanAction.APPROVE,
            ingested_offset=0,
        )
    # Belt and braces: even an entry built AROUND validation is refused.
    forced = MemoryEntry.model_construct(
        schema_version=1,
        entry_id="mem-forced",
        source_gen_id="g-9",
        scenario_id="S-B-77",
        pool="B",
        signature={"has_unapplied_cash": True},
        weight=float(1),
        final_text="text",
        human_action=HumanAction.APPROVE,
        note="",
        ingested_offset=0,
    )
    with pytest.raises(ValueError, match="I7"):
        store.add(forced)
    assert store.entries() == []


def test_mixed_pool_log_adds_only_pool_a(paths: dict[str, Path], cfg: Config) -> None:
    write_fixture(paths["fixtures"], make_ledger("S-A-77", "A"))
    write_fixture(paths["fixtures"], make_ledger("S-B-77", "B"))
    append_event(decision("S-A-77", "A", HumanAction.APPROVE, gen_id="g-1"), paths["log"])
    append_event(decision("S-B-77", "B", HumanAction.EDIT, gen_id="g-2"), paths["log"])
    append_event(decision("S-A-77", "A", HumanAction.EDIT, gen_id="g-3"), paths["log"])
    store = MemoryStore(paths["store"])

    result = ingest(paths["log"], store, paths["meta"], paths["fixtures"], cfg)

    assert result["added"] == 2
    assert result["refused_pool_b"] == 1
    entries = store.entries()
    assert len(entries) == 2
    assert all(e.pool == "A" for e in entries)
    assert {e.scenario_id for e in entries} == {"S-A-77"}
    assert {e.source_gen_id for e in entries} == {"g-1", "g-3"}
