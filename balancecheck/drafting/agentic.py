"""Bounded agentic revision recovery: the tool-loop orchestrator.

What this module guarantees:

- The agent is evidence-bounded. Its prompts never contain the rendered
  ledger block; every figure it may state must be gathered through the
  read-only tools in balancecheck.drafting.tools, and required coverage
  (account summary, open invoices, unapplied sources when the account has
  any, and the specific documents that failed verification) is enforced in
  CODE before a final draft is accepted. If the model never gathers the
  required evidence, the orchestrator executes the missing tools itself
  (round_index -1) before the forced final call.
- The agent never decides. This module returns draft text and bookkeeping
  only; the caller routes the draft back through extract_claims, check_all,
  and decide. No gate action is ever read from, shown to, or emitted by the
  agent.
- Budgets are enforced in code, never trusted to the model: at most
  cfg.agentic_max_rounds tool rounds plus one forced final LLM call, at most
  min(3, cfg.agentic_max_calls_per_round) calls per round, and at most
  cfg.agentic_max_total_tool_calls executed tool calls in total. Structural
  step rules (kind "tool_calls" needs an empty draft and at least one call;
  kind "final" needs no calls and a non-empty draft) are re-checked here
  even though the schema already constrains decoding.
- Every tool ATTEMPT appends exactly one ToolCallEvent to the event log:
  executed calls, cache hits, budget rejections, and malformed calls alike.
  An identical (name, canonical arguments) repeat is served from a per-run
  cache (ok true, "cached": true inside data) without re-execution and
  without consuming the total-tool budget, but it is still logged.
- Determinism: temperature 0, canonical JSON for dedup keys and transcript
  rendering, and tools that are pure functions of the Ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from balancecheck.config import Config
from balancecheck.contracts.models import Ledger, ToolCallEvent
from balancecheck.drafting.tools import execute_tool, tool_catalog
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import append_event
from balancecheck.spine.trace import prompt_sha as compute_prompt_sha
from balancecheck.substrate import derive

# The schema handed to constrained decoding for every agent step. The
# structural rules it implies are ALSO enforced in code below; the schema is
# a decode-level guard, not the trust boundary.
AGENT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["tool_calls", "final"]},
        "reason": {"type": "string", "maxLength": 300},
        "tool_calls": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            },
        },
        "draft": {"type": "string"},
    },
    "required": ["kind", "reason", "tool_calls", "draft"],
    "additionalProperties": False,
}

# The schema's per-step call ceiling; cfg may only tighten it, never widen.
_SCHEMA_MAX_CALLS = 3

_MISSING_EVIDENCE_PREFIX = "final rejected: missing required evidence: "

_SYSTEM_PROMPT = """You are a senior accounts-receivable assistant at an accounting firm. A
drafted balance-due reply failed deterministic verification. Produce a
corrected reply grounded ONLY in evidence returned by the read-only account
tools listed in the user message.

Protocol: at each step return exactly one JSON object matching the required
schema. Set kind to "tool_calls" (with an empty draft and one to three tool
calls) to gather evidence, or kind to "final" (with an empty tool_calls list
and the complete corrected reply in draft) once the evidence supports every
statement. The reason field is a short operational note about the step, one
sentence at most.

Evidence rules:
- Rely only on tool results shown in the transcript. Never invent or guess
  a document ID, an amount, a date, or a status.
- Copy every amount exactly as the tools render it.
- If a tool call fails, adjust and continue; never fabricate its answer.

The final draft must follow the reply format exactly:
1. Greet the client contact by name.
2. State the client's total balance due exactly once, in the exact form
   $X,XXX.XX, and never repeat the total anywhere else in the reply.
3. Itemize every open invoice by its verbatim ID with its open amount, and
   write every date in ISO format (YYYY-MM-DD).
4. Explicitly mention every unapplied payment and every unapplied credit
   memo by its verbatim ID and amount.
5. Refer to documents only by their verbatim IDs (the INV-, PMT-, and CM-
   forms).
6. Never use percentages. Never spell an amount in words; every amount
   appears only in the $X,XXX.XX form.
7. Make no statement about conversations, agreements, approvals, or
   promises.
8. Do not use em dashes.
9. Keep the tone professional, warm, and concise.
10. Sign off as "Accounts Receivable" with no personal name."""


@dataclass
class AgenticResult:
    """The orchestrator's bookkeeping for one agentic recovery attempt.

    text is "" when no acceptable final draft was produced; the caller
    treats that as a failed recovery. steps counts agent LLM calls made
    (failed and invalid steps included). coverage_forced lists the tool
    names executed by code (round_index -1), not chosen by the model.
    prompt_sha is the sha of the accepted step's user prompt ("" when no
    step was accepted).
    """

    text: str
    steps: int
    tool_calls_executed: int
    forced_final: bool
    coverage_forced: list[str]
    prompt_sha: str


@dataclass(frozen=True)
class _RequiredCall:
    """One coverage requirement: the call to make and how to describe it."""

    name: str
    arguments: dict[str, Any]
    label: str
    note: str


# ---------------------------------------------------------------------------
# Canonicalization and coverage
# ---------------------------------------------------------------------------


def _canonical_args(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _required_evidence(ledger: Ledger, failed_subject_ids: list[str]) -> list[_RequiredCall]:
    """The coverage the agent must gather before a final draft is accepted.

    The unapplied-sources requirement is computed from the same derive
    functions the summary tool wraps, so "the summary's unapplied totals are
    nonzero" and this predicate agree by construction.
    """
    required: list[_RequiredCall] = [
        _RequiredCall("get_account_summary", {}, "get_account_summary", "always required"),
        _RequiredCall("list_open_invoices", {}, "list_open_invoices", "always required"),
    ]
    if derive.unapplied_cash_total(ledger) != 0 or derive.unapplied_credit_total(ledger) != 0:
        required.append(
            _RequiredCall(
                "list_unapplied_sources",
                {},
                "list_unapplied_sources",
                "the account has unapplied cash or credit",
            )
        )
    seen: set[str] = set()
    for raw in failed_subject_ids:
        doc_id = raw.strip()
        upper = doc_id.upper()
        if not doc_id or upper in seen:
            continue
        seen.add(upper)
        if upper.startswith("INV-"):
            required.append(
                _RequiredCall(
                    "get_invoice",
                    {"id": doc_id},
                    f"get_invoice({doc_id})",
                    "this document failed verification",
                )
            )
        elif upper.startswith("PMT-") or upper.startswith("CM-"):
            required.append(
                _RequiredCall(
                    "get_source",
                    {"id": doc_id},
                    f"get_source({doc_id})",
                    "this document failed verification",
                )
            )
    return required


def _satisfied(req: _RequiredCall, successes: list[tuple[str, dict[str, Any]]]) -> bool:
    """Whether a successful attempt covers the requirement.

    Per-document requirements match on the ID appearing among the argument
    values, so a model call with a differently named argument key still
    counts once the tool accepted it.
    """
    if not req.arguments:
        return any(name == req.name for name, _args in successes)
    wanted = str(req.arguments.get("id", "")).upper()
    for name, args in successes:
        if name != req.name:
            continue
        if any(str(value).upper() == wanted for value in args.values()):
            return True
    return False


# ---------------------------------------------------------------------------
# Tool session: execution, caching, budgets, and the one-event-per-attempt log
# ---------------------------------------------------------------------------


class _ToolSession:
    """Mutable per-recovery state: transcript, cache, budgets, event log."""

    def __init__(
        self,
        cfg: Config,
        ledger: Ledger,
        executor: Callable[[str, dict[str, Any], Ledger], dict[str, Any]],
        gen_id: str,
        revision_index: int,
        run_id: str,
    ) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.executor = executor
        self.gen_id = gen_id
        self.revision_index = revision_index
        self.run_id = run_id
        self.transcript: list[dict[str, Any]] = []
        self.successes: list[tuple[str, dict[str, Any]]] = []
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.budget_used = 0        # executed model-chosen calls (cache hits excluded)
        self.executed_count = 0     # executor invocations, forced coverage included
        self.attempts = 0           # every attempt, one ToolCallEvent each

    def note(self, text: str) -> None:
        self.transcript.append({"note": text})

    def attempt(
        self,
        name: Any,
        arguments: Any,
        round_index: int,
        call_index: int,
        *,
        forced: bool = False,
        over_round_budget: bool = False,
    ) -> bool:
        """Process one tool-call attempt; exactly one ToolCallEvent per call."""
        tool_name = str(name or "")
        args: dict[str, Any] = dict(arguments) if isinstance(arguments, dict) else {}
        ok = False
        error = ""
        result: dict[str, Any]
        if not tool_name:
            error = "invalid tool call: missing tool name"
            result = {"ok": False, "error": error}
        elif not isinstance(arguments, dict):
            error = "invalid tool call: arguments must be an object"
            result = {"ok": False, "error": error}
        elif over_round_budget:
            error = "per-round tool budget exceeded"
            result = {"ok": False, "error": error}
        else:
            key = (tool_name, _canonical_args(args))
            if key in self.cache:
                data = dict(self.cache[key].get("data") or {})
                data["cached"] = True
                result = {"ok": True, "data": data}
                ok = True
            elif not forced and self.budget_used >= self.cfg.agentic_max_total_tool_calls:
                error = "total tool budget exhausted"
                result = {"ok": False, "error": error}
            else:
                if not forced:
                    self.budget_used += 1
                self.executed_count += 1
                try:
                    raw = self.executor(tool_name, args, self.ledger)
                except Exception as exc:  # a tool bug must not kill the recovery
                    raw = {"ok": False, "error": f"tool raised {type(exc).__name__}: {exc}"}
                result = raw if isinstance(raw, dict) else {"ok": False, "error": "non-dict tool result"}
                ok = bool(result.get("ok"))
                error = "" if ok else str(result.get("error", "tool call failed"))
                if ok:
                    self.cache[key] = result
        if ok:
            self.successes.append((tool_name, args))
        append_event(
            ToolCallEvent(
                run_id=self.run_id,
                gen_id=self.gen_id,
                revision_index=self.revision_index,
                round_index=round_index,
                call_index=call_index,
                tool_name=tool_name,
                arguments=args,
                result=result,
                ok=ok,
                error=error,
            ),
            self.cfg.log_path,
        )
        self.attempts += 1
        self.transcript.append({"call": tool_name, "arguments": args, "result": result})
        return ok


# ---------------------------------------------------------------------------
# Prompt assembly (rebuilt in full every step; never the rendered ledger)
# ---------------------------------------------------------------------------


def _catalog_text() -> str:
    catalog = tool_catalog()
    if isinstance(catalog, str):
        return catalog
    return _compact_json(catalog)


def _exemplars_block(exemplars: list[Any]) -> str:
    texts: list[str] = []
    for entry in exemplars or []:
        text = getattr(entry, "final_text", None)
        if text is None:
            text = str(entry)
        text = text.strip()
        if text:
            texts.append(text)
    if not texts:
        return ""
    parts = ["Examples of replies our reviewers approved for similar accounts:"]
    for n, text in enumerate(texts, start=1):
        parts.append(f"--- EXAMPLE {n} START ---")
        parts.append(text)
        parts.append(f"--- EXAMPLE {n} END ---")
    return "\n".join(parts)


def _transcript_text(transcript: list[dict[str, Any]]) -> str:
    if not transcript:
        return "(no tool calls yet)"
    lines: list[str] = []
    for entry in transcript:
        if "note" in entry:
            lines.append(f"NOTE: {entry['note']}")
        else:
            lines.append(
                f"CALL {entry['call']} {_compact_json(entry['arguments'])}"
                f" -> {_compact_json(entry['result'])}"
            )
    return "\n".join(lines)


def _build_step_prompt(
    previous_draft: str,
    decision_reason: str,
    correction: str,
    exemplars: list[Any],
    catalog_text: str,
    required: list[_RequiredCall],
    transcript: list[dict[str, Any]],
    rounds_remaining: int,
    calls_per_round: int,
    total_calls_remaining: int,
    forced_final: bool,
) -> str:
    sections: list[str] = [
        "A drafted balance-due reply failed deterministic verification."
        " Gather evidence with the tools, then return the complete corrected reply.",
        "PREVIOUS DRAFT (failed verification):\n" + previous_draft.strip(),
        "VERIFICATION DECISION:\n" + decision_reason.strip(),
        "REQUIRED CORRECTION (fix exactly this):\n" + correction.strip(),
    ]
    exemplar_block = _exemplars_block(exemplars)
    if exemplar_block:
        sections.append(exemplar_block)
    sections.append("AVAILABLE TOOLS (read-only account records):\n" + catalog_text)
    sections.append(
        "REQUIRED EVIDENCE (each call must succeed before a final draft is accepted):\n"
        + "\n".join(f"- {req.label} ({req.note})" for req in required)
    )
    sections.append("TOOL TRANSCRIPT SO FAR:\n" + _transcript_text(transcript))
    sections.append(
        "REMAINING BUDGETS:\n"
        f"- tool rounds remaining (including this step): {rounds_remaining}\n"
        f"- max tool calls per round: {calls_per_round}\n"
        f"- total tool calls remaining: {total_calls_remaining}"
    )
    if forced_final:
        sections.append(
            "NO TOOL ROUNDS REMAIN. You MUST return kind=\"final\" now, with an"
            " empty tool_calls list and the complete corrected draft in the"
            " draft field."
        )
    else:
        sections.append(
            "Return exactly one JSON step now: kind \"tool_calls\" to gather"
            " missing evidence, or kind \"final\" with the complete corrected"
            " draft once every required piece of evidence has succeeded."
        )
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Step validation (the schema's structural rules, re-enforced in code)
# ---------------------------------------------------------------------------


def _validate_step(parsed: Any) -> tuple[str, list[Any], str, str]:
    """Returns (kind, tool_calls, draft, problem); problem == "" when valid."""
    if not isinstance(parsed, dict):
        return "", [], "", "step is not a JSON object"
    kind = parsed.get("kind")
    tool_calls = parsed.get("tool_calls")
    draft = parsed.get("draft")
    if kind not in ("tool_calls", "final"):
        return "", [], "", "unknown step kind"
    if not isinstance(tool_calls, list) or not isinstance(draft, str):
        return "", [], "", "step does not match the required shape"
    if kind == "tool_calls":
        if draft.strip():
            return kind, tool_calls, draft, "kind=tool_calls must carry an empty draft"
        if not tool_calls:
            return kind, tool_calls, draft, "kind=tool_calls requires at least one call"
        return kind, tool_calls, draft, ""
    if tool_calls:
        return kind, tool_calls, draft, "kind=final must carry no tool calls"
    if not draft.strip():
        return kind, tool_calls, draft, "kind=final requires a non-empty draft"
    return kind, tool_calls, draft, ""


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def generate_agentic_revision(
    client: ModelClient,
    cfg: Config,
    ledger: Ledger,
    previous_draft: str,
    correction: str,
    decision_reason: str,
    failed_subject_ids: list[str],
    exemplars: list[Any],
    gen_id: str,
    revision_index: int,
    run_id: str = "",
    stub_prefix: str = "run",
    executor: Callable[[str, dict[str, Any], Ledger], dict[str, Any]] = execute_tool,
) -> AgenticResult:
    """Run one bounded agentic recovery and return the corrected draft text.

    The returned text is NEVER trusted here: the caller must route it back
    through extract_claims / check_all / decide. This function only
    orchestrates evidence gathering under hard budgets and logs every tool
    attempt; on any unrecoverable failure it returns text == "".
    """
    session = _ToolSession(cfg, ledger, executor, gen_id, revision_index, run_id)
    catalog_text = _catalog_text()
    required = _required_evidence(ledger, failed_subject_ids)
    calls_per_round = min(_SCHEMA_MAX_CALLS, cfg.agentic_max_calls_per_round)
    total_rounds = max(0, cfg.agentic_max_rounds)
    steps = 0

    def call_model(prompt: str) -> Any:
        return client.complete(
            task="agentic_revision",
            prompt=prompt,
            system=_SYSTEM_PROMPT,
            schema=AGENT_STEP_SCHEMA,
            temperature=0,
            max_tokens=cfg.agentic_max_tokens,
            stub_key=f"{stub_prefix}:{ledger.scenario_id}:agent:{steps}",
            meta={"gen_id": gen_id, "step": steps},
        )

    def missing_labels() -> list[str]:
        return [req.label for req in required if not _satisfied(req, session.successes)]

    for round_index in range(total_rounds):
        prompt = _build_step_prompt(
            previous_draft,
            decision_reason,
            correction,
            exemplars,
            catalog_text,
            required,
            session.transcript,
            rounds_remaining=total_rounds - round_index,
            calls_per_round=calls_per_round,
            total_calls_remaining=max(
                0, cfg.agentic_max_total_tool_calls - session.budget_used
            ),
            forced_final=False,
        )
        resp = call_model(prompt)
        steps += 1
        if not getattr(resp, "ok", False):
            continue  # failed step; the client already traced it
        kind, tool_calls, draft, problem = _validate_step(getattr(resp, "parsed", None))
        if problem:
            continue  # violating step: treated as no tool calls requested
        if kind == "final":
            missing = missing_labels()
            if not missing:
                return AgenticResult(
                    text=draft.strip(),
                    steps=steps,
                    tool_calls_executed=session.executed_count,
                    forced_final=False,
                    coverage_forced=[],
                    prompt_sha=compute_prompt_sha(prompt),
                )
            session.note(_MISSING_EVIDENCE_PREFIX + ", ".join(missing))
            continue
        for call_index, call in enumerate(tool_calls):
            name: Any = ""
            arguments: Any = {}
            if isinstance(call, dict):
                name = call.get("name", "")
                arguments = call.get("arguments", {})
            session.attempt(
                name,
                arguments,
                round_index,
                call_index,
                over_round_budget=call_index >= calls_per_round,
            )

    # Rounds exhausted: force-execute the missing required evidence in code
    # (round_index -1), then demand the final draft.
    coverage_forced: list[str] = []
    forced_index = 0
    for req in required:
        if _satisfied(req, session.successes):
            continue
        session.attempt(req.name, req.arguments, -1, forced_index, forced=True)
        coverage_forced.append(req.name)
        forced_index += 1

    prompt = _build_step_prompt(
        previous_draft,
        decision_reason,
        correction,
        exemplars,
        catalog_text,
        required,
        session.transcript,
        rounds_remaining=0,
        calls_per_round=calls_per_round,
        total_calls_remaining=0,
        forced_final=True,
    )
    resp = call_model(prompt)
    steps += 1
    if getattr(resp, "ok", False):
        kind, _tool_calls, draft, problem = _validate_step(getattr(resp, "parsed", None))
        if not problem and kind == "final":
            return AgenticResult(
                text=draft.strip(),
                steps=steps,
                tool_calls_executed=session.executed_count,
                forced_final=True,
                coverage_forced=coverage_forced,
                prompt_sha=compute_prompt_sha(prompt),
            )
    return AgenticResult(
        text="",
        steps=steps,
        tool_calls_executed=session.executed_count,
        forced_final=True,
        coverage_forced=coverage_forced,
        prompt_sha="",
    )
