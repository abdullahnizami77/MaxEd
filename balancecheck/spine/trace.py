"""The trace hook: one JSON line per model-call attempt (invariant I6).

The function signature the brief asks for, exactly: trace(task, prompt,
output, meta). The model client calls it from a finally block around every
attempt, so a timeout, a retry, or an exception still emits its line. The
trace lines live in the same event log as the learning signal; every
inference is training signal.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from balancecheck.contracts.models import TraceEvent
from balancecheck.spine.events import append_event


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def trace(task: str, prompt: str, output: str, meta: dict, *, log_path: Path) -> int:
    """Append one trace line; returns the log offset."""
    meta = dict(meta or {})
    event = TraceEvent(
        task=task,
        prompt_sha=prompt_sha(prompt),
        prompt=prompt,
        output=output,
        model_name=str(meta.pop("model_name", "")),
        attempt=int(meta.pop("attempt", 1)),
        ok=bool(meta.pop("ok", True)),
        error=str(meta.pop("error", "")),
        run_id=str(meta.pop("run_id", "")),
        meta=meta,
    )
    return append_event(event, log_path)
