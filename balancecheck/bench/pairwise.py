"""The pairwise judge protocol: both orderings, inconsistency collapses to a tie.

What this module guarantees:

- Every pair is judged twice, once with draft a as Draft 1 (ordering "ab")
  and once with draft a as Draft 2 (ordering "ba"), and each ordering's raw
  winner is mapped back to the underlying draft ("a" | "b" | "tie") before
  the two are compared, so position never leaks into the verdict.
- The two orderings must agree about the underlying drafts for a decisive
  verdict; any disagreement (including a parse failure on either call)
  collapses conservatively to "tie" with flipped=True. The resulting flip
  rate is therefore reported as an UPPER BOUND on position bias, not a
  measurement of it: some flips are legitimate near-ties, and a parse
  failure counts as a flip here even though it is not positional.
- Each ordering appends exactly one JudgmentEvent (mode "pairwise") with its
  own ordering, its own mapped verdict, and its own prompt_sha, so the log
  carries both raw judgments, not only the corrected one.
- The prompt lives in prompts/judge_pairwise.md (versioned, diffable,
  hash-attributable); this module only fills its placeholders
  ({{ledger_block}}, {{draft_1}}, {{draft_2}}).

No money flows through this module; no float literal appears here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from balancecheck.bench.judge import load_template
from balancecheck.config import REPO_ROOT, Config
from balancecheck.contracts.models import JudgmentEvent
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import append_event
from balancecheck.spine.trace import prompt_sha as compute_prompt_sha

PAIRWISE_TEMPLATE_PATH: Path = REPO_ROOT / "prompts" / "judge_pairwise.md"

# The constrained-decoding schema for one pairwise verdict.
PAIRWISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "enum": ["1", "2", "tie"]},
        "rationale": {"type": "string"},
    },
    "required": ["winner", "rationale"],
    "additionalProperties": False,
}

ORDERINGS: tuple[str, str] = ("ab", "ba")


def map_winner(winner: str, ordering: str) -> str:
    """Map a positional winner ("1" | "2" | "tie") back to "a" | "b" | "tie".

    In ordering "ab", draft a is Draft 1; in ordering "ba", draft a is
    Draft 2. This mapping is the whole point of running both orders: the
    comparison downstream is between drafts, never between positions.
    """
    if winner == "tie":
        return "tie"
    if winner not in ("1", "2"):
        raise ValueError(f"unknown pairwise winner {winner!r}")
    if ordering == "ab":
        return "a" if winner == "1" else "b"
    if ordering == "ba":
        return "b" if winner == "1" else "a"
    raise ValueError(f"unknown ordering {ordering!r}")


def build_pairwise_prompt(
    draft_1: str, draft_2: str, ledger_block: str
) -> tuple[str, str]:
    """Assemble the (system, user) prompt pair for one ordering."""
    system, user = load_template(PAIRWISE_TEMPLATE_PATH)
    user = user.replace("{{ledger_block}}", ledger_block)
    user = user.replace("{{draft_1}}", draft_1)
    user = user.replace("{{draft_2}}", draft_2)
    return system, user


def pairwise_both_orders(
    client: ModelClient,
    cfg: Config,
    pair_id: str,
    draft_a: str,
    draft_b: str,
    ledger_block: str,
    stub_key_prefix: str,
    run_id: str = "",
) -> dict[str, Any]:
    """Judge one pair in both orders; disagreement collapses to a tie.

    Two model calls (task "judge_pairwise", temperature 0, PAIRWISE_SCHEMA
    constrained decoding): ordering "ab" shows draft a as Draft 1, ordering
    "ba" shows draft a as Draft 2. Each ordering's winner is mapped back to
    the underlying draft and appended as its own JudgmentEvent (mode
    "pairwise") to cfg.log_path; a failed call still appends its event with
    parse_ok False and an empty verdict.

    If the two mapped verdicts agree, that verdict stands with
    flipped=False. If they disagree in ANY way (including a parse failure on
    either ordering), the verdict is "tie" with flipped=True: the
    conservative correction. The flip rate aggregated from `flipped` is
    reported as an UPPER BOUND on position bias, not a measurement of it,
    because some flips are legitimate near-ties.

    Returns {"verdict": "a" | "b" | "tie", "flipped": bool,
             "orderings": {"ab": {"verdict": str, "parse_ok": bool},
                           "ba": {"verdict": str, "parse_ok": bool}}}.
    """
    orderings: dict[str, dict[str, Any]] = {}
    for ordering in ORDERINGS:
        if ordering == "ab":
            first, second = draft_a, draft_b
        else:
            first, second = draft_b, draft_a
        system, user = build_pairwise_prompt(first, second, ledger_block)
        resp = client.complete(
            task="judge_pairwise",
            prompt=user,
            system=system,
            schema=PAIRWISE_SCHEMA,
            stub_key=f"{stub_key_prefix}:{ordering}",
            meta={"pair_id": pair_id, "ordering": ordering},
            temperature=0,
        )
        if resp.ok and isinstance(resp.parsed, dict):
            verdict = map_winner(str(resp.parsed["winner"]), ordering)
            parse_ok = True
            scores = dict(resp.parsed)
        else:
            verdict = ""
            parse_ok = False
            scores = {}
        event = JudgmentEvent(
            run_id=run_id,
            pair_id=pair_id,
            mode="pairwise",
            ordering=ordering,
            scores=scores,
            verdict=verdict,
            prompt_sha=compute_prompt_sha(user),
            parse_ok=parse_ok,
            judge_model=cfg.model or "stub",
        )
        append_event(event, cfg.log_path)
        orderings[ordering] = {"verdict": verdict, "parse_ok": parse_ok}

    ab = orderings["ab"]
    ba = orderings["ba"]
    if ab["parse_ok"] and ba["parse_ok"] and ab["verdict"] == ba["verdict"]:
        return {"verdict": ab["verdict"], "flipped": False, "orderings": orderings}
    return {"verdict": "tie", "flipped": True, "orderings": orderings}
