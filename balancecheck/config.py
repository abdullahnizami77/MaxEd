"""Runtime configuration, environment-driven.

Stub mode is the default: the whole system runs end to end with zero
credentials and no network (the brief requires it, invariant tests rely on
it). Live mode is enabled explicitly via BC_MODE=live plus BC_BASE_URL.
Nothing deployment-specific is hardcoded anywhere in this repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    mode: str = "stub"  # "stub" | "live"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    log_path: Path = field(default_factory=lambda: REPO_ROOT / "log" / "events.jsonl")
    stub_file: Path = field(default_factory=lambda: REPO_ROOT / "fixtures" / "stub_responses.json")
    seed: int = 20260718
    timeout_s: float = 240.0
    max_attempts: int = 3
    # Agentic revision recovery (off by default: the fast path is unchanged).
    # "prompt" replays the existing single-call revision; "agentic" lets a
    # bounded, read-only tool-using agent produce the revision instead. The
    # agent never decides: its draft goes back through extract/check/decide.
    revision_mode: str = "prompt"  # "prompt" | "agentic"
    agentic_max_recoveries: int = 1
    agentic_max_rounds: int = 2
    agentic_max_calls_per_round: int = 3
    agentic_max_total_tool_calls: int = 6
    agentic_max_tokens: int = 1600


def load_config(**overrides) -> Config:
    """Build a Config from BC_* environment variables, with overrides."""
    env = {
        "mode": os.environ.get("BC_MODE", "stub"),
        "base_url": os.environ.get("BC_BASE_URL", ""),
        "model": os.environ.get("BC_MODEL", ""),
        "api_key": os.environ.get("BC_API_KEY", ""),
        "seed": int(os.environ.get("BC_SEED", "20260718")),
        "timeout_s": float(os.environ.get("BC_TIMEOUT_S", "240")),
        "max_attempts": int(os.environ.get("BC_MAX_ATTEMPTS", "3")),
        "revision_mode": os.environ.get("BC_REVISION_MODE", "prompt"),
        "agentic_max_recoveries": int(os.environ.get("BC_AGENTIC_MAX_RECOVERIES", "1")),
        "agentic_max_rounds": int(os.environ.get("BC_AGENTIC_MAX_ROUNDS", "2")),
        "agentic_max_calls_per_round": int(os.environ.get("BC_AGENTIC_MAX_CALLS_PER_ROUND", "3")),
        "agentic_max_total_tool_calls": int(os.environ.get("BC_AGENTIC_MAX_TOTAL_TOOL_CALLS", "6")),
        "agentic_max_tokens": int(os.environ.get("BC_AGENTIC_MAX_TOKENS", "1600")),
    }
    if "BC_LOG" in os.environ:
        env["log_path"] = Path(os.environ["BC_LOG"])
    if "BC_STUB_FILE" in os.environ:
        env["stub_file"] = Path(os.environ["BC_STUB_FILE"])
    env.update(overrides)
    cfg = Config(**env)
    if cfg.mode == "live" and not cfg.base_url:
        raise ValueError("BC_MODE=live requires BC_BASE_URL to be set")
    if cfg.mode not in ("stub", "live"):
        raise ValueError(f"unknown BC_MODE {cfg.mode!r}; expected 'stub' or 'live'")
    if cfg.revision_mode not in ("prompt", "agentic"):
        raise ValueError(
            f"unknown BC_REVISION_MODE {cfg.revision_mode!r}; expected 'prompt' or 'agentic'"
        )
    if cfg.revision_mode == "agentic" and cfg.mode == "stub":
        # The agent's tool choices are model behaviour; canned stubs would
        # fake exactly the thing being evaluated. Orchestration is unit
        # tested with scripted clients instead (tests construct Config
        # directly, which this env-driven guard does not restrict).
        raise ValueError("BC_REVISION_MODE=agentic requires BC_MODE=live")
    return cfg
