"""The per-signature-class cap: at most 3 entries per exact bit tuple, oldest
(lowest ingested_offset) evicted first, evicted ids reported to the caller.

Hermetic: a tmp_path store, entries constructed inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from balancecheck.contracts.models import STRUCTURE_LABELS, HumanAction, MemoryEntry
from balancecheck.memory.store import CLASS_CAP, MemoryStore, class_key


def sig(*true_labels: str) -> dict[str, bool]:
    unknown = set(true_labels) - set(STRUCTURE_LABELS)
    assert not unknown, f"typo in test labels: {unknown}"
    return {label: label in true_labels for label in STRUCTURE_LABELS}


def make_entry(
    entry_id: str, offset: int, signature: dict[str, bool], weight: float | None = None
) -> MemoryEntry:
    return MemoryEntry(
        entry_id=entry_id,
        source_gen_id=f"g-{entry_id}",
        scenario_id="S-A-77",
        pool="A",
        signature=signature,
        weight=float(1) if weight is None else weight,
        final_text=f"draft text for {entry_id}",
        human_action=HumanAction.APPROVE,
        ingested_offset=offset,
    )


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.json")


def test_cap_is_three_and_first_added_is_evicted(store: MemoryStore) -> None:
    s = sig("has_unapplied_cash")
    assert store.add(make_entry("m0", 0, s)) == []
    assert store.add(make_entry("m1", 1, s)) == []
    assert store.add(make_entry("m2", 2, s)) == []
    evicted = store.add(make_entry("m3", 3, s))
    assert evicted == ["m0"], "the oldest entry of the class must be evicted"
    entries = store.entries()
    assert len(entries) == CLASS_CAP
    assert {e.entry_id for e in entries} == {"m1", "m2", "m3"}


def test_eviction_is_by_lowest_offset_not_insertion_order(store: MemoryStore) -> None:
    s = sig("has_partial")
    store.add(make_entry("m-late", 9, s))
    store.add(make_entry("m-early", 2, s))
    store.add(make_entry("m-mid", 5, s))
    evicted = store.add(make_entry("m-new", 7, s))
    assert evicted == ["m-early"]
    assert {e.entry_id for e in store.entries()} == {"m-late", "m-mid", "m-new"}


def test_other_signature_classes_are_untouched(store: MemoryStore) -> None:
    cash = sig("has_unapplied_cash")
    credit = sig("has_open_credit")
    for n in range(CLASS_CAP):
        assert store.add(make_entry(f"cash-{n}", n, cash)) == []
    assert store.add(make_entry("credit-0", 10, credit)) == []
    evicted = store.add(make_entry("cash-3", 11, cash))
    assert evicted == ["cash-0"]
    ids = {e.entry_id for e in store.entries()}
    assert "credit-0" in ids, "eviction must never cross signature classes"
    assert len(ids) == CLASS_CAP + 1


def test_class_key_is_the_exact_bit_tuple(store: MemoryStore) -> None:
    # Two signatures that overlap but differ in one bit are different classes.
    a = sig("has_unapplied_cash", "has_partial")
    b = sig("has_unapplied_cash")
    assert class_key(a) != class_key(b)
    for n in range(CLASS_CAP):
        store.add(make_entry(f"a-{n}", n, a))
    assert store.add(make_entry("b-0", 20, b)) == []
    assert len(store.entries()) == CLASS_CAP + 1
