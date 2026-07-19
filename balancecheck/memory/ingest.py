"""Idempotent ingest: human decisions become memory, exactly once (plan 13, I7).

What this module guarantees:

- Idempotency by consumed-offset: a meta file records the log offset the
  ingest has consumed through; each call reads only events at or beyond it,
  so calling ingest twice with no new events adds nothing the second time.
- Pool B never enters memory (invariant I7): any human decision whose pool
  is not "A" is refused and counted, never converted to an entry. The store
  enforces the same rule again on add(), so leakage would need two
  independent failures.
- Every consumed EDIT or APPROVE on Pool A becomes one MemoryEntry with the
  signature DETECTED from the scenario's fixture ledger (derive.signature),
  weight 2.0 for EDIT and 1.0 for APPROVE, and the event's log line offset
  as ingested_offset, so recency ordering is grounded in the log itself.
- Ledgers are loaded by reading fixtures/<scenario_id>.json directly through
  the Ledger contract: the fixture file format is the stable contract, so
  ingest does not depend on the foundry module being present.
- Every call appends exactly one IngestEvent to the log (entries added and
  evicted, the new consumed offset, and the store's state hash), so the
  learning loop's consumption is itself observable in the spine.

Stated limitation (v1): DECLINE decisions are skipped, not consumed. A
decline is captured signal (it stays in the log and is counted by this
function), but v1 deliberately does not ingest negative exemplars; deciding
how a declined draft should steer retrieval is future work, and pretending
otherwise by wedging declines into the same weighted-exemplar shape would
mislead more than it would teach.
"""

from __future__ import annotations

import json
from pathlib import Path

from balancecheck.config import Config
from balancecheck.contracts.models import (
    HumanAction,
    HumanDecisionEvent,
    IngestEvent,
    Ledger,
    MemoryEntry,
    parse_event,
)
from balancecheck.memory.store import MemoryStore
from balancecheck.spine.events import append_event, line_count
from balancecheck.substrate import derive

# Edit carries more signal than approve: the human paid the cost of fixing
# the text, so the corrected exemplar outranks a merely acceptable one.
_WEIGHT_BY_ACTION: dict[HumanAction, float] = {
    HumanAction.EDIT: float(2),
    HumanAction.APPROVE: float(1),
}


def read_consumed_offset(meta_path: Path) -> int:
    """The offset ingest has consumed through; 0 when the meta file is absent."""
    if not meta_path.exists():
        return 0
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    return int(data.get("consumed_through_offset", 0))


def _write_consumed_offset(meta_path: Path, offset: int) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"consumed_through_offset": offset}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _events_with_offsets(log_path: Path, start_offset: int) -> list[tuple[int, object]]:
    """(line offset, parsed event) pairs from start_offset on.

    Mirrors spine.events.read_events line accounting exactly (blank lines
    are skipped but still occupy an offset), so offsets recorded here always
    agree with spine.events.line_count.
    """
    if not log_path.exists():
        return []
    pairs: list[tuple[int, object]] = []
    with log_path.open("r", encoding="utf-8") as f:
        for n, line in enumerate(f):
            if n < start_offset:
                continue
            stripped = line.strip()
            if not stripped:
                continue
            pairs.append((n, parse_event(stripped)))
    return pairs


def _load_ledger(scenario_id: str, fixtures_dir: Path) -> Ledger:
    """Read fixtures/<scenario_id>.json through the Ledger contract.

    The fixture file is the stable contract between the foundry and every
    consumer; reading it directly keeps ingest free of a foundry import.
    """
    path = fixtures_dir / f"{scenario_id}.json"
    return Ledger.model_validate_json(path.read_text(encoding="utf-8"))


def ingest(
    log_path: Path,
    store: MemoryStore,
    meta_path: Path,
    fixtures_dir: Path,
    cfg: Config,
) -> dict[str, int]:
    """Consume new human decisions from the log into the memory store.

    Returns {"added", "evicted", "refused_pool_b", "skipped_decline"}.
    cfg is part of the stable pipeline interface (every stage takes the run
    Config); ingest itself needs no model and reads nothing from it in v1.
    """
    del cfg  # interface stability only; see docstring
    consumed_from = read_consumed_offset(meta_path)

    added = 0
    evicted = 0
    refused_pool_b = 0
    skipped_decline = 0

    # Two-phase for crash safety: build every entry first (ledger loads are
    # where failures happen), and only when the whole batch is constructed
    # touch the store and the offset. A crash mid-batch therefore adds
    # nothing and advances nothing, so the rerun cannot double-add.
    pending: list[MemoryEntry] = []
    for offset, event in _events_with_offsets(log_path, consumed_from):
        if not isinstance(event, HumanDecisionEvent):
            continue
        if event.pool != "A":
            # Invariant I7: evaluation scenarios never enter memory. Anything
            # that is not provably Pool A is refused, not just literal "B".
            refused_pool_b += 1
            continue
        if event.action == HumanAction.DECLINE:
            skipped_decline += 1
            continue
        ledger = _load_ledger(event.scenario_id, fixtures_dir)
        if ledger.pool != "A":
            # Second I7 layer: the fixture file is the authority, not the
            # event's self-reported pool string.
            refused_pool_b += 1
            continue
        pending.append(
            MemoryEntry(
                entry_id=f"mem-{offset:06d}",
                source_gen_id=event.gen_id,
                scenario_id=event.scenario_id,
                pool="A",
                signature=derive.signature(ledger),
                weight=_WEIGHT_BY_ACTION[event.action],
                final_text=event.final_text,
                human_action=event.action,
                note=event.reason,
                ingested_offset=offset,
            )
        )
    for entry in pending:
        evicted += len(store.add(entry))
        added += 1

    consumed_through = line_count(log_path)
    _write_consumed_offset(meta_path, consumed_through)
    append_event(
        IngestEvent(
            consumed_through_offset=consumed_through,
            entries_added=added,
            entries_evicted=evicted,
            memory_state_hash=store.state_hash(),
        ),
        log_path,
    )
    return {
        "added": added,
        "evicted": evicted,
        "refused_pool_b": refused_pool_b,
        "skipped_decline": skipped_decline,
    }
