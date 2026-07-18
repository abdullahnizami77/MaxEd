"""The event log: one JSONL, append-only, schema-versioned (invariant I8).

Every module either writes to this log or reads from it; no module talks to
another except through typed records. spine/report.py is the only consumer
that produces human-readable numbers.
"""

from __future__ import annotations

from pathlib import Path

from balancecheck.contracts.models import dump_event, parse_event


def append_event(event, log_path: Path) -> int:
    """Append one event; returns the zero-based line offset it landed on."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    if log_path.exists():
        with log_path.open("rb") as f:
            offset = sum(1 for _ in f)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(dump_event(event) + "\n")
    return offset


def read_events(log_path: Path, *, start_offset: int = 0) -> list:
    """Parse every line through the discriminated union; a malformed line
    raises, because a log that cannot be trusted line-by-line is not a log."""
    if not log_path.exists():
        return []
    events = []
    with log_path.open("r", encoding="utf-8") as f:
        for n, line in enumerate(f):
            if n < start_offset:
                continue
            line = line.strip()
            if not line:
                continue
            events.append(parse_event(line))
    return events


def line_count(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    with log_path.open("rb") as f:
        return sum(1 for _ in f)
