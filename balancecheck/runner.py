"""The loop: generate, verify, decide; revise at most twice; nothing auto-sends.

One scenario in, one terminal outcome out (revise is never terminal). Every
step lands in the event log as a typed record: generation, verification,
score (first_pass and final stages), capability gap. The before/after
comparison reads the first_pass score events, because the revise loop scrubs
final drafts toward correctness and would mask learning (plan v3, section 13).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from balancecheck.bench.score import build_score_event
from balancecheck.checks.checks import check_all
from balancecheck.config import Config
from balancecheck.contracts.models import (
    GateAction,
    GateDecision,
    GenerationEvent,
    Ledger,
    VerificationEvent,
)
from balancecheck.drafting.drafter import generate_draft
from balancecheck.drafting.surface import extract_claims
from balancecheck.gate.gaps import derive_gap
from balancecheck.gate.policy import decide, precheck_abstain
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import append_event
from balancecheck.substrate import derive

MAX_REVISIONS = 2

# The terminal reason when the single bounded agentic recovery still leaves
# a draft needing revision (or produced none): never loop, always hand to a
# human. gate/gaps.py maps unrecognized escalate reasons to OTHER, so the
# capability-gap record still emits.
AGENTIC_EXHAUSTED = "agentic_recovery_exhausted"


@dataclass
class ScenarioOutcome:
    scenario_id: str
    gen_id: str
    terminal_action: str
    final_draft: str
    revision_count: int
    reason: str


def run_scenario(
    cfg: Config,
    client: ModelClient,
    ledger: Ledger,
    *,
    pass_label: str,
    run_id: str,
    memory=None,
    stub_prefix: str = "run",
) -> ScenarioOutcome:
    gen_base = f"{pass_label}-{ledger.scenario_id}"

    # Ledger-level ambiguity is checked before any drafting: when the records
    # cannot support a correct draft, the honest move is to not draft at all.
    pre = precheck_abstain(ledger)
    if pre is not None:
        gen_id = f"{gen_base}-r0"
        append_event(
            VerificationEvent(gen_id=gen_id, claims=[], decision=pre, run_id=run_id),
            cfg.log_path,
        )
        gap = derive_gap(pre, ledger, gen_id)
        if gap is not None:
            gap.run_id = run_id
            append_event(gap, cfg.log_path)
        for stage in ("first_pass", "final"):
            append_event(
                build_score_event(
                    gen_id=gen_id,
                    scenario_id=ledger.scenario_id,
                    pass_label=pass_label,
                    stage=stage,
                    claims=[],
                    draft="",
                    ledger=ledger,
                    revision_count=0,
                    terminal_action=pre.action.value,
                    run_id=run_id,
                ),
                cfg.log_path,
            )
        return ScenarioOutcome(
            scenario_id=ledger.scenario_id,
            gen_id=gen_id,
            terminal_action=pre.action.value,
            final_draft="",
            revision_count=0,
            reason=pre.reason,
        )

    exemplars = list(memory.retrieve(derive.signature(ledger), 3)) if memory else []
    draft_hashes: list[str] = []
    correction = ""
    previous_draft = ""
    previous_reason = ""
    previous_failed_ids: list[str] = []
    agentic_recoveries = 0

    def _terminal(gen_id, decision, claims, draft, rev):
        gap = derive_gap(decision, ledger, gen_id)
        if gap is not None:
            gap.run_id = run_id
            append_event(gap, cfg.log_path)
        append_event(
            build_score_event(
                gen_id=gen_id,
                scenario_id=ledger.scenario_id,
                pass_label=pass_label,
                stage="final",
                claims=claims,
                draft=draft,
                ledger=ledger,
                revision_count=rev,
                terminal_action=decision.action.value,
                run_id=run_id,
            ),
            cfg.log_path,
        )
        return ScenarioOutcome(
            scenario_id=ledger.scenario_id,
            gen_id=gen_id,
            terminal_action=decision.action.value,
            final_draft=draft,
            revision_count=rev,
            reason=decision.reason,
        )

    for rev in range(MAX_REVISIONS + 1):
        gen_id = f"{gen_base}-r{rev}"

        if rev == 0:
            generation_mode = "prompt_initial"
        elif (
            cfg.revision_mode == "agentic"
            and agentic_recoveries < cfg.agentic_max_recoveries
        ):
            generation_mode = "agentic_recovery"
        else:
            generation_mode = "prompt_revision"

        if generation_mode == "agentic_recovery":
            from balancecheck.drafting.agentic import generate_agentic_revision

            agentic_recoveries += 1
            ar = generate_agentic_revision(
                client=client,
                cfg=cfg,
                ledger=ledger,
                previous_draft=previous_draft,
                correction=correction,
                decision_reason=previous_reason,
                failed_subject_ids=previous_failed_ids,
                exemplars=exemplars,
                gen_id=gen_id,
                revision_index=rev,
                run_id=run_id,
                stub_prefix=stub_prefix,
            )
            if not ar.text:
                # The bounded agent could not produce a draft within its
                # budgets. Nothing is retried autonomously; the scenario
                # escalates to a human with the last correction attached.
                decision = GateDecision(
                    action=GateAction.ESCALATE,
                    reason=AGENTIC_EXHAUSTED,
                    payload=correction,
                )
                append_event(
                    VerificationEvent(
                        gen_id=gen_id, claims=[], decision=decision, run_id=run_id
                    ),
                    cfg.log_path,
                )
                return _terminal(gen_id, decision, [], "", rev)
            draft_text, prompt_sha = ar.text, ar.prompt_sha
        else:
            result = generate_draft(
                client,
                ledger,
                exemplars,
                correction=correction,
                stub_key=f"{stub_prefix}:{ledger.scenario_id}:draft:{rev}",
                meta={"gen_id": gen_id, "pass_label": pass_label},
            )
            draft_text, prompt_sha = result.text, result.prompt_sha

        append_event(
            GenerationEvent(
                gen_id=gen_id,
                scenario_id=ledger.scenario_id,
                pool=ledger.pool,
                pass_label=pass_label,
                ledger_ref=ledger.ref(),
                prompt_sha=prompt_sha,
                draft=draft_text,
                revision_index=rev,
                is_first_pass=rev == 0,
                generation_mode=generation_mode,
                run_id=run_id,
            ),
            cfg.log_path,
        )
        claims = extract_claims(draft_text, ledger, claim_prefix=gen_id)
        check_all(
            claims,
            ledger,
            client,
            stub_key_prefix=f"{stub_prefix}:{ledger.scenario_id}:r{rev}",
        )
        draft_hashes.append(hashlib.sha256(draft_text.encode()).hexdigest())
        decision = decide(
            claims, ledger, revision_index=rev, draft_hashes=draft_hashes,
            draft=draft_text,
        )
        # In agentic mode there is exactly one bounded recovery: a draft that
        # still needs revision after it terminates as an escalation rather
        # than looping. The conversion happens before the decision is logged,
        # so the record shows the decision that actually governed.
        if (
            decision.action == GateAction.REVISE
            and cfg.revision_mode == "agentic"
            and agentic_recoveries >= cfg.agentic_max_recoveries
            and rev > 0
        ):
            decision = GateDecision(
                action=GateAction.ESCALATE,
                reason=AGENTIC_EXHAUSTED,
                findings=decision.findings,
                payload=decision.payload,
            )
        append_event(
            VerificationEvent(gen_id=gen_id, claims=claims, decision=decision, run_id=run_id),
            cfg.log_path,
        )
        if rev == 0:
            append_event(
                build_score_event(
                    gen_id=gen_id,
                    scenario_id=ledger.scenario_id,
                    pass_label=pass_label,
                    stage="first_pass",
                    claims=claims,
                    draft=draft_text,
                    ledger=ledger,
                    revision_count=0,
                    terminal_action=decision.action.value,
                    run_id=run_id,
                ),
                cfg.log_path,
            )
        if decision.action == GateAction.REVISE:
            # The gate's decision is the single source of the revision
            # instruction. Every REVISE row (claim-level corrections and the
            # aggregate itemization/completeness rows alike) builds a
            # prompt-ready payload; rebuilding it from failed claims here
            # would send an empty correction for whole-draft failures, which
            # have no failed individual claim.
            correction = decision.payload
            previous_draft = draft_text
            previous_reason = decision.reason
            previous_failed_ids = _failed_subject_ids(claims)
            continue

        return _terminal(gen_id, decision, claims, draft_text, rev)

    raise RuntimeError(
        "revision loop exceeded its budget without a terminal decision; "
        "policy.decide must escalate at the budget boundary"
    )


def _failed_subject_ids(claims) -> list[str]:
    """Documents named by claims that did not pass, for the agent's required
    evidence coverage (deduplicated, reading order)."""
    out: list[str] = []
    for c in claims:
        if c.check_result is None or c.check_result.status.value == "pass":
            continue
        for sid in c.subject_ids or ([c.subject_id] if c.subject_id else []):
            if sid and sid not in out:
                out.append(sid)
    return out
