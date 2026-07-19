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

    for rev in range(MAX_REVISIONS + 1):
        gen_id = f"{gen_base}-r{rev}"
        result = generate_draft(
            client,
            ledger,
            exemplars,
            correction=correction,
            stub_key=f"{stub_prefix}:{ledger.scenario_id}:draft:{rev}",
            meta={"gen_id": gen_id, "pass_label": pass_label},
        )
        append_event(
            GenerationEvent(
                gen_id=gen_id,
                scenario_id=ledger.scenario_id,
                pool=ledger.pool,
                pass_label=pass_label,
                ledger_ref=ledger.ref(),
                prompt_sha=result.prompt_sha,
                draft=result.text,
                revision_index=rev,
                is_first_pass=rev == 0,
                run_id=run_id,
            ),
            cfg.log_path,
        )
        claims = extract_claims(result.text, ledger, claim_prefix=gen_id)
        check_all(
            claims,
            ledger,
            client,
            stub_key_prefix=f"{stub_prefix}:{ledger.scenario_id}:r{rev}",
        )
        draft_hashes.append(hashlib.sha256(result.text.encode()).hexdigest())
        decision = decide(
            claims, ledger, revision_index=rev, draft_hashes=draft_hashes,
            draft=result.text,
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
                    draft=result.text,
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
            continue

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
                draft=result.text,
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
            final_draft=result.text,
            revision_count=rev,
            reason=decision.reason,
        )

    raise RuntimeError(
        "revision loop exceeded its budget without a terminal decision; "
        "policy.decide must escalate at the budget boundary"
    )
