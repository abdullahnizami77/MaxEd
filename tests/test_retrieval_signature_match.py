"""Retrieval ranking: signature overlap first, then weight (edit beats
approve), then recency (ingested_offset), then entry_id, and k is respected.

Hermetic: a tmp_path store, entries constructed inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from balancecheck.contracts.models import STRUCTURE_LABELS, HumanAction, MemoryEntry
from balancecheck.memory.store import MemoryStore

EDIT_WEIGHT = float(2)
APPROVE_WEIGHT = float(1)


def sig(*true_labels: str) -> dict[str, bool]:
    unknown = set(true_labels) - set(STRUCTURE_LABELS)
    assert not unknown, f"typo in test labels: {unknown}"
    return {label: label in true_labels for label in STRUCTURE_LABELS}


def make_entry(
    entry_id: str,
    signature: dict[str, bool],
    *,
    weight: float | None = None,
    offset: int = 0,
    action: HumanAction = HumanAction.APPROVE,
) -> MemoryEntry:
    return MemoryEntry(
        entry_id=entry_id,
        source_gen_id=f"g-{entry_id}",
        scenario_id="S-A-77",
        pool="A",
        signature=signature,
        weight=APPROVE_WEIGHT if weight is None else weight,
        final_text=f"draft text for {entry_id}",
        human_action=action,
        ingested_offset=offset,
    )


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.json")


def test_overlap_dominates_weight_and_recency(store: MemoryStore) -> None:
    # The lower-overlap entries get higher weight AND higher recency, so a
    # correct ranking can only come from overlap being the primary key.
    store.add(
        make_entry(
            "two-bits",
            sig("has_unapplied_cash", "has_partial"),
            weight=APPROVE_WEIGHT,
            offset=1,
        )
    )
    store.add(
        make_entry(
            "one-bit",
            sig("has_unapplied_cash", "has_open_credit"),
            weight=EDIT_WEIGHT,
            offset=5,
            action=HumanAction.EDIT,
        )
    )
    store.add(make_entry("zero-bits", sig("has_cutoff_risk"), weight=EDIT_WEIGHT, offset=9))
    query = sig("has_unapplied_cash", "has_partial")
    ranked = store.retrieve(query, k=3)
    assert [e.entry_id for e in ranked] == ["two-bits", "one-bit", "zero-bits"]


def test_overlap_counts_only_labels_true_in_both(store: MemoryStore) -> None:
    # An entry with many true bits the query does not share gains nothing.
    store.add(
        make_entry(
            "busy",
            sig("has_open_credit", "has_near_duplicate", "has_cutoff_risk"),
            offset=9,
        )
    )
    store.add(make_entry("on-point", sig("has_partial"), offset=1))
    ranked = store.retrieve(sig("has_partial"), k=2)
    assert [e.entry_id for e in ranked] == ["on-point", "busy"]


def test_weight_breaks_overlap_ties_edit_beats_approve(store: MemoryStore) -> None:
    s = sig("has_open_credit")
    # The approve entry is MORE recent; weight must still win the tie.
    store.add(make_entry("approved", s, weight=APPROVE_WEIGHT, offset=8))
    store.add(
        make_entry("edited", s, weight=EDIT_WEIGHT, offset=2, action=HumanAction.EDIT)
    )
    ranked = store.retrieve(s, k=2)
    assert [e.entry_id for e in ranked] == ["edited", "approved"]


def test_recency_breaks_remaining_ties(store: MemoryStore) -> None:
    s = sig("has_near_duplicate")
    store.add(make_entry("older", s, offset=3))
    store.add(make_entry("newer", s, offset=7))
    ranked = store.retrieve(s, k=2)
    assert [e.entry_id for e in ranked] == ["newer", "older"]


def test_entry_id_makes_the_order_total(store: MemoryStore) -> None:
    s = sig("has_cutoff_risk")
    store.add(make_entry("b-entry", s, offset=4))
    store.add(make_entry("a-entry", s, offset=4))
    ranked = store.retrieve(s, k=2)
    assert [e.entry_id for e in ranked] == ["a-entry", "b-entry"]


def test_k_is_respected(store: MemoryStore) -> None:
    for n, label in enumerate(("has_unapplied_cash", "has_partial", "has_open_credit")):
        store.add(make_entry(f"e-{n}", sig(label), offset=n))
    assert len(store.retrieve(sig("has_unapplied_cash"), k=2)) == 2
    assert len(store.retrieve(sig("has_unapplied_cash"), k=3)) == 3
    assert store.retrieve(sig("has_unapplied_cash"), k=0) == []
    # k larger than the store returns everything, never errors.
    assert len(store.retrieve(sig("has_unapplied_cash"), k=10)) == 3
