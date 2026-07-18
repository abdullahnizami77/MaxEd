"""Fixture invariants: the foundry's ledgers are deterministic, structurally
valid, independently recomputable (invariant I2), and honestly labeled (the
stored structure_labels equal what derive.signature() detects).

Hermetic: builds every ledger in-process and writes only under tmp_path.
"""

from __future__ import annotations

import json

import pytest

from balancecheck.contracts.models import STRUCTURE_LABELS, Ledger
from balancecheck.substrate import derive
from balancecheck.substrate.foundry import (
    build_all,
    load_ledger,
    load_pool,
    write_fixtures,
)

EXPECTED_LABELS_BY_ARCHETYPE = {
    "01": [],
    "02": ["has_unapplied_cash"],
    "03": ["has_partial"],
    "04": ["has_open_credit"],
    "05": ["has_near_duplicate", "has_cutoff_risk"],
    "06": ["has_unapplied_cash", "has_near_duplicate", "has_ambiguous_allocation"],
}


@pytest.fixture(scope="module")
def ledgers() -> list[Ledger]:
    return build_all()


def test_build_all_is_deterministic() -> None:
    first = build_all()
    second = build_all()
    assert [l.scenario_id for l in first] == [l.scenario_id for l in second]
    for a, b in zip(first, second):
        assert a.model_dump_json() == b.model_dump_json(), (
            f"{a.scenario_id}: two builds are not byte-identical"
        )


def test_twelve_unique_scenarios_six_per_pool(ledgers: list[Ledger]) -> None:
    ids = [l.scenario_id for l in ledgers]
    assert len(ids) == 12
    assert len(set(ids)) == 12
    expected = {f"S-A-{n:02d}" for n in range(1, 7)} | {f"S-B-{n:02d}" for n in range(1, 7)}
    assert set(ids) == expected
    assert sum(1 for l in ledgers if l.pool == "A") == 6
    assert sum(1 for l in ledgers if l.pool == "B") == 6


def test_pool_identities(ledgers: list[Ledger]) -> None:
    for ledger in ledgers:
        if ledger.pool == "A":
            assert ledger.client.id == "CL-A"
            assert ledger.client.name == "Harbor & Finch Design Co."
            assert str(ledger.client.as_of) == "2026-03-31"
        else:
            assert ledger.client.id == "CL-B"
            assert ledger.client.name == "Bluestem Orchard Supply LLC"
            assert str(ledger.client.as_of) == "2026-04-30"


def test_every_ledger_validates_clean(ledgers: list[Ledger]) -> None:
    for ledger in ledgers:
        problems = derive.validate_ledger(ledger)
        assert problems == [], f"{ledger.scenario_id}: {problems}"


def test_net_balance_matches_independent_naive_recompute(ledgers: list[Ledger]) -> None:
    for ledger in ledgers:
        raw = json.loads(ledger.model_dump_json())
        assert derive.net_balance(ledger) == derive.recompute_net_balance_naive(raw), (
            f"{ledger.scenario_id}: derive and naive recompute disagree"
        )


def test_stored_labels_equal_detected_signature(ledgers: list[Ledger]) -> None:
    for ledger in ledgers:
        sig = derive.signature(ledger)
        detected = [label for label in STRUCTURE_LABELS if sig[label]]
        assert ledger.structure_labels == detected, (
            f"{ledger.scenario_id}: stored {ledger.structure_labels}, detected {detected}"
        )


def test_archetypes_carry_their_designed_labels(ledgers: list[Ledger]) -> None:
    for ledger in ledgers:
        archetype = ledger.scenario_id[-2:]
        assert ledger.structure_labels == EXPECTED_LABELS_BY_ARCHETYPE[archetype], (
            f"{ledger.scenario_id}: labels drifted from the designed archetype"
        )


def test_clean_baselines_have_all_false_signatures(ledgers: list[Ledger]) -> None:
    for ledger in ledgers:
        if not ledger.scenario_id.endswith("-01"):
            continue
        sig = derive.signature(ledger)
        assert not any(sig.values()), f"{ledger.scenario_id}: expected all six bits false, got {sig}"
        assert ledger.structure_labels == []


def test_ambiguous_scenarios_have_exactly_one_two_candidate_ambiguity(
    ledgers: list[Ledger],
) -> None:
    for ledger in ledgers:
        if not ledger.scenario_id.endswith("-06"):
            continue
        amb = derive.ambiguous_allocations(ledger)
        assert len(amb) == 1, f"{ledger.scenario_id}: expected one ambiguity, got {amb}"
        payment_id, candidates = amb[0]
        assert len(candidates) == 2, f"{ledger.scenario_id}: expected two candidates"
        assert derive.unapplied_amount(ledger, payment_id) > 0


def test_write_then_load_round_trips(tmp_path, ledgers: list[Ledger]) -> None:
    write_fixtures(tmp_path)
    for ledger in ledgers:
        loaded = load_ledger(ledger.scenario_id, tmp_path)
        assert loaded == ledger
        assert loaded.model_dump_json() == ledger.model_dump_json()


def test_load_pool_returns_each_pool_in_index_order(tmp_path, ledgers: list[Ledger]) -> None:
    write_fixtures(tmp_path)
    pool_a = load_pool("A", tmp_path)
    pool_b = load_pool("B", tmp_path)
    assert [l.scenario_id for l in pool_a] == [f"S-A-{n:02d}" for n in range(1, 7)]
    assert [l.scenario_id for l in pool_b] == [f"S-B-{n:02d}" for n in range(1, 7)]
    assert all(l.pool == "A" for l in pool_a)
    assert all(l.pool == "B" for l in pool_b)
    with pytest.raises(ValueError):
        load_pool("C", tmp_path)


def test_index_rows_match_ledgers(tmp_path, ledgers: list[Ledger]) -> None:
    write_fixtures(tmp_path)
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 12
    by_id = {l.scenario_id: l for l in ledgers}
    for row in index:
        assert set(row) == {"scenario_id", "pool", "structure_labels", "ref"}
        ledger = by_id[row["scenario_id"]]
        assert row["pool"] == ledger.pool
        assert row["structure_labels"] == ledger.structure_labels
        assert row["ref"] == ledger.ref()
