"""Structure-keyed retrieval memory: the consumption mechanism (plan section 13).

What this module guarantees:

- Only Pool A entries are ever stored (invariant I7). The MemoryEntry
  contract already pins pool to Literal["A"]; add() re-checks at runtime as
  belt and braces, so an entry constructed around validation still cannot
  enter memory.
- Capacity is bounded per signature class: at most CLASS_CAP entries whose
  signature maps to the same bit tuple over STRUCTURE_LABELS. When a class
  overflows, the OLDEST entry (lowest ingested_offset, entry_id as the
  deterministic tiebreak) is evicted and its id is reported to the caller,
  so eviction is observable, never silent.
- Retrieval is a deterministic total order: signature overlap (count of
  labels true in both the query and the entry) descending, then weight
  descending (edit 2.0 beats approve 1.0), then ingested_offset descending
  (recency), then entry_id ascending as the final tiebreak.
- state_hash() matches spine/manifest.memory_state_hash semantics exactly
  (it delegates to it): "empty" when the store holds no entries, otherwise
  a hash of the canonical file bytes, so the pass1/pass2 manifest diff and
  the ingest event agree on what memory state a run saw.
- The file on disk is canonical JSON (sorted keys, fixed indentation), so
  identical contents always produce identical bytes and identical hashes.
"""

from __future__ import annotations

import json
from pathlib import Path

from balancecheck.contracts.models import STRUCTURE_LABELS, MemoryEntry
from balancecheck.spine.manifest import memory_state_hash

# At most this many exemplars per signature class (exact bit tuple over
# STRUCTURE_LABELS). Oldest-out keeps the store bounded and fresh.
CLASS_CAP = 3


def class_key(signature: dict[str, bool]) -> tuple[int, ...]:
    """The signature class: the exact bit tuple over STRUCTURE_LABELS.

    Labels absent from the dict count as False, so a partial signature still
    maps to a well-defined class.
    """
    return tuple(int(bool(signature.get(label, False))) for label in STRUCTURE_LABELS)


def signature_overlap(query: dict[str, bool], candidate: dict[str, bool]) -> int:
    """Count of structure labels true in BOTH signatures."""
    return sum(
        1
        for label in STRUCTURE_LABELS
        if bool(query.get(label, False)) and bool(candidate.get(label, False))
    )


class MemoryStore:
    """A JSON-file-backed list of MemoryEntry records with capped classes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def entries(self) -> list[MemoryEntry]:
        """Every stored entry, validated through the contract on the way in."""
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        raw = json.loads(text)
        return [MemoryEntry.model_validate(item) for item in raw]

    def _write(self, entries: list[MemoryEntry]) -> None:
        """Write the canonical file: sorted keys, fixed indent, trailing newline."""
        payload = [e.model_dump(mode="json") for e in entries]
        blob = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(blob, encoding="utf-8")

    def state_hash(self) -> str:
        """Delegates to spine/manifest.memory_state_hash: "empty" when no
        entries, otherwise the hash of the canonical file bytes."""
        return memory_state_hash(self.path)

    def clear(self) -> None:
        """Reset to the canonical empty store (state_hash returns "empty")."""
        self._write([])

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, entry: MemoryEntry) -> list[str]:
        """Append one entry; enforce the per-class cap; return evicted ids.

        Raises ValueError for any non-Pool-A entry (invariant I7), even one
        constructed around Pydantic validation.
        """
        if entry.pool != "A":
            raise ValueError(
                f"memory refuses pool {entry.pool!r} entry {entry.entry_id!r}: "
                "only Pool A enters memory (invariant I7)"
            )
        entries = self.entries()
        entries.append(entry)
        key = class_key(entry.signature)
        evicted: list[str] = []
        while sum(1 for e in entries if class_key(e.signature) == key) > CLASS_CAP:
            oldest = min(
                (e for e in entries if class_key(e.signature) == key),
                key=lambda e: (e.ingested_offset, e.entry_id),
            )
            entries.remove(oldest)
            evicted.append(oldest.entry_id)
        self._write(entries)
        return evicted

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(self, sig: dict[str, bool], k: int = 3) -> list[MemoryEntry]:
        """Top-k entries for a query signature, in a deterministic total order:
        overlap desc, weight desc, ingested_offset desc, entry_id asc."""
        if k <= 0:
            return []
        ranked = sorted(
            self.entries(),
            key=lambda e: (
                -signature_overlap(sig, e.signature),
                -e.weight,
                -e.ingested_offset,
                e.entry_id,
            ),
        )
        return ranked[:k]
