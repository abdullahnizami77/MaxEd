"""The LLM pointwise judge: rubric scores on what code cannot check.

What this module guarantees:

- The judge scores only tone, clarity, and acceptability (completeness of
  explanation and tone, never arithmetic: grounding is a code-computed
  metric elsewhere), and every score carries a cited span, so a judgment is
  auditable back to the draft text that produced it.
- The prompt lives in prompts/judge_rubric.md (versioned, diffable,
  hash-attributable); this module only fills its placeholders
  ({{ledger_block}}, {{draft}}), so the prompt_sha recorded on the
  JudgmentEvent matches the trace line for the same call.
- Every call to judge_draft appends exactly one JudgmentEvent (mode
  "rubric") to the configured log, including failed calls: a judge that
  cannot produce valid scores still leaves its parse_ok=False line, because
  the malformed rate is a first-class reported number, not a hidden one.
- JUDGE_SCHEMA is proper JSON Schema with integer minimum/maximum bounds,
  used for constrained decoding in live mode and jsonschema validation in
  both modes, so an out-of-range score is a parse failure, never a silent
  outlier in the statistics.

No money flows through this module; no float literal appears here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from balancecheck.config import REPO_ROOT, Config
from balancecheck.contracts.models import JudgmentEvent
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import append_event
from balancecheck.spine.trace import prompt_sha as compute_prompt_sha

RUBRIC_TEMPLATE_PATH: Path = REPO_ROOT / "prompts" / "judge_rubric.md"

# The constrained-decoding schema for the rubric verdict. minimum/maximum
# make an out-of-range score a decode-level impossibility in live mode and a
# validation failure in stub mode.
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tone": {"type": "integer", "minimum": 1, "maximum": 4},
        "tone_span": {"type": "string"},
        "clarity": {"type": "integer", "minimum": 1, "maximum": 4},
        "clarity_span": {"type": "string"},
        "acceptable": {"type": "boolean"},
        "acceptable_rationale": {"type": "string"},
    },
    "required": [
        "tone",
        "tone_span",
        "clarity",
        "clarity_span",
        "acceptable",
        "acceptable_rationale",
    ],
    "additionalProperties": False,
}

# Section markers are line-anchored so prose mentions of the markers (for
# example in a template's own header comment) never split the file.
_SECTION_RE = re.compile(r"^\[(SYSTEM|USER)\][ \t]*$", re.MULTILINE)


def load_template(path: Path) -> tuple[str, str]:
    """Split a prompt template file into its (system, user) sections.

    Shared by the rubric and pairwise judges; raises ValueError when either
    section line is missing, because a half-loaded prompt must never be sent.
    """
    text = path.read_text(encoding="utf-8")
    markers = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        sections[m.group(1)] = text[m.end() : end].strip()
    if "SYSTEM" not in sections or "USER" not in sections:
        raise ValueError(f"template {path} must contain [SYSTEM] and [USER] section lines")
    return sections["SYSTEM"], sections["USER"]


def build_rubric_prompt(draft: str, ledger_block: str) -> tuple[str, str]:
    """Assemble the (system, user) prompt pair for one rubric judgment."""
    system, user = load_template(RUBRIC_TEMPLATE_PATH)
    user = user.replace("{{ledger_block}}", ledger_block)
    user = user.replace("{{draft}}", draft)
    return system, user


def judge_draft(
    client: ModelClient,
    cfg: Config,
    gen_id: str,
    draft: str,
    ledger_block: str,
    stub_key: str,
    run_id: str = "",
) -> dict[str, Any]:
    """Judge one draft against its records block on the rubric dimensions.

    Makes one logical model call (task "judge_rubric") at temperature 0 with
    JUDGE_SCHEMA constrained decoding, then appends one JudgmentEvent (mode
    "rubric") to cfg.log_path regardless of outcome: a failed or malformed
    call still leaves its event with parse_ok False and empty scores.

    Returns {"scores": <parsed dict or None>, "parse_ok": <bool>}.
    """
    system, user = build_rubric_prompt(draft, ledger_block)
    resp = client.complete(
        task="judge_rubric",
        prompt=user,
        system=system,
        schema=JUDGE_SCHEMA,
        stub_key=stub_key,
        meta={"gen_id": gen_id},
        temperature=0,
    )
    parsed: dict[str, Any] | None = (
        resp.parsed if resp.ok and isinstance(resp.parsed, dict) else None
    )
    parse_ok = bool(resp.ok)
    event = JudgmentEvent(
        run_id=run_id,
        gen_id=gen_id,
        mode="rubric",
        scores=parsed or {},
        prompt_sha=compute_prompt_sha(user),
        parse_ok=parse_ok,
        judge_model=cfg.model or "stub",
    )
    append_event(event, cfg.log_path)
    return {"scores": parsed, "parse_ok": parse_ok}
