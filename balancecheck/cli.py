"""Command-line entry points: every Makefile gate lands here.

The commands mirror the loop: make-stubs and demo prove the system with no
model and zero credentials; run-pool / review / ingest close the learning
loop; errors, bench-*, report, and readme-inject produce the evidence. The
calibration selection is blind by construction: the labeler-facing set file
carries no tier or corruption information, and the truth mapping is written
sealed into results/raw for unsealing only after labels.csv is committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

from balancecheck.config import REPO_ROOT, load_config
from balancecheck.contracts.models import (
    GenerationEvent,
    HumanAction,
    HumanDecisionEvent,
    Ledger,
    RunStartEvent,
    VerificationEvent,
)
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import append_event, read_events
from balancecheck.spine.manifest import (
    config_hash,
    git_sha,
    new_run_id,
    write_manifest,
)

FIXTURES = REPO_ROOT / "fixtures"
RESULTS = REPO_ROOT / "results"
RAW = RESULTS / "raw"
LABELS = REPO_ROOT / "labels"
MEMORY_FILE = REPO_ROOT / "memory_store.json"
MEMORY_META = REPO_ROOT / "memory_meta.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_pool(pool: str) -> list[Ledger]:
    from balancecheck.substrate.foundry import load_pool

    return load_pool(pool, FIXTURES)


def _load_ledger(scenario_id: str) -> Ledger:
    return Ledger.model_validate_json((FIXTURES / f"{scenario_id}.json").read_text())


def _client(cfg, run_id: str = "") -> ModelClient:
    return ModelClient(cfg=cfg, run_id=run_id)


def _emit_run_start(cfg, run_id: str, pass_label: str, pool: str, mem_hash: str) -> None:
    append_event(
        RunStartEvent(
            run_id=run_id,
            pass_label=pass_label,
            seed=cfg.seed,
            git_sha=git_sha(),
            model_name=cfg.model or "stub",
            model_digest=cfg.model or "stub",
            config_hash=config_hash(cfg),
            pool=pool,
            memory_state_hash=mem_hash,
        ),
        cfg.log_path,
    )


class RandomRetrievalStore:
    """The pass2R ablation: same memory contents, retrieval replaced by a
    seeded random draw of matched count, so the delta between pass2 and
    pass2R isolates the retrieval function, not the presence of examples."""

    def __init__(self, store, seed: int):
        self._entries = list(store.entries())
        self._rng = random.Random(seed)

    def retrieve(self, sig, k: int = 3):
        if not self._entries:
            return []
        k = min(k, len(self._entries))
        return self._rng.sample(self._entries, k)


# ---------------------------------------------------------------------------
# stubs and demo
# ---------------------------------------------------------------------------


def cmd_make_stubs(_args) -> int:
    from balancecheck.drafting.golden import golden_draft
    from balancecheck.substrate.foundry import load_pool

    stubs: dict[str, dict] = {}
    for pool in ("A", "B"):
        for ledger in load_pool(pool, FIXTURES):
            text = golden_draft(ledger)
            for prefix in ("demo", "stub", ledger.pool):
                for rev in range(3):
                    stubs[f"{prefix}:{ledger.scenario_id}:draft:{rev}"] = {"text": text}
    out = FIXTURES / "stub_responses.json"
    out.write_text(json.dumps(stubs, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(stubs)} stub responses to {out}")
    return 0


def cmd_demo(_args) -> int:
    from balancecheck.runner import run_scenario

    cfg = load_config(mode="stub")
    run_id = new_run_id("demo")
    _emit_run_start(cfg, run_id, "demo", "A", "empty")
    client = _client(cfg, run_id)
    outcomes = []
    for sid in ("S-A-01", "S-A-06"):
        ledger = _load_ledger(sid)
        outcome = run_scenario(
            cfg, client, ledger, pass_label="demo", run_id=run_id, stub_prefix="demo"
        )
        outcomes.append(outcome)
        print(f"{sid}: {outcome.terminal_action} ({outcome.reason})")
    actions = {o.scenario_id: o.terminal_action for o in outcomes}
    ok = actions.get("S-A-01") == "human_gate" and actions.get("S-A-06") == "abstain"
    print(
        "demo "
        + ("PASSED" if ok else "FAILED")
        + ": clean scenario reaches the human gate, ambiguous scenario abstains; "
        + f"model calls: {client.call_count}, all traced to {cfg.log_path}"
    )
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def cmd_run_pool(args) -> int:
    from balancecheck.memory.store import MemoryStore
    from balancecheck.runner import run_scenario

    cfg = load_config()
    memory = None
    mem_hash = "empty"
    if args.with_memory or args.with_random_memory:
        store = MemoryStore(MEMORY_FILE)
        mem_hash = store.state_hash()
        memory = RandomRetrievalStore(store, cfg.seed) if args.with_random_memory else store
    run_id = new_run_id(args.pass_label)
    write_manifest(
        run_id, args.pass_label, cfg, pool=args.pool, mem_hash=mem_hash
    )
    _emit_run_start(cfg, run_id, args.pass_label, args.pool, mem_hash)
    client = _client(cfg, run_id)
    for ledger in _load_pool(args.pool):
        outcome = run_scenario(
            cfg,
            client,
            ledger,
            pass_label=args.pass_label,
            run_id=run_id,
            memory=memory,
            stub_prefix=args.pass_label if cfg.mode == "live" else "stub",
        )
        print(
            f"{ledger.scenario_id}: {outcome.terminal_action} "
            f"after {outcome.revision_count} revisions ({outcome.reason})"
        )
    print(f"run {run_id} complete; manifest in runs/{run_id}/")
    return 0


def cmd_review(args) -> int:
    cfg = load_config()
    events = read_events(cfg.log_path)
    decided = {e.gen_id for e in events if isinstance(e, HumanDecisionEvent)}
    pending: dict[str, tuple[GenerationEvent, VerificationEvent]] = {}
    gens = {e.gen_id: e for e in events if isinstance(e, GenerationEvent)}
    for ev in events:
        if isinstance(ev, VerificationEvent) and ev.decision.action.value == "human_gate":
            if ev.gen_id not in decided and ev.gen_id in gens:
                pending[ev.gen_id] = (gens[ev.gen_id], ev)
    if args.list:
        for gen_id, (gen, ver) in sorted(pending.items()):
            print("=" * 72)
            print(f"gen_id: {gen_id}  scenario: {gen.scenario_id}  pool: {gen.pool}")
            print(f"checks: {len(ver.claims)} claims, decision: {ver.decision.reason}")
            print("-" * 72)
            print(gen.draft)
        print("=" * 72)
        print(f"{len(pending)} generation(s) awaiting a human decision")
        return 0
    if args.apply:
        decisions = json.loads(Path(args.apply).read_text())
        applied = 0
        for row in decisions:
            gen_id = row["gen_id"]
            if gen_id not in pending and gen_id not in gens:
                print(f"skipping unknown gen_id {gen_id}")
                continue
            gen = gens[gen_id]
            action = HumanAction(row["action"])
            final_text = row.get("final_text", "")
            if action == HumanAction.APPROVE and not final_text:
                final_text = gen.draft
            append_event(
                HumanDecisionEvent(
                    gen_id=gen_id,
                    scenario_id=gen.scenario_id,
                    pool=gen.pool,
                    action=action,
                    final_text=final_text if action != HumanAction.DECLINE else "",
                    reason=row.get("reason", ""),
                ),
                cfg.log_path,
            )
            applied += 1
        print(f"applied {applied} human decision(s)")
        return 0
    print("use --list to see pending drafts or --apply decisions.json")
    return 1


def cmd_ingest(_args) -> int:
    from balancecheck.memory.ingest import ingest
    from balancecheck.memory.store import MemoryStore

    cfg = load_config()
    store = MemoryStore(MEMORY_FILE)
    result = ingest(cfg.log_path, store, MEMORY_META, FIXTURES, cfg)
    print(json.dumps(result, indent=2))
    print(f"memory state hash: {store.state_hash()}")
    return 0


# ---------------------------------------------------------------------------
# evidence: injected errors
# ---------------------------------------------------------------------------


def _verify_draft(cfg, client, ledger, draft: str, tag: str) -> dict:
    """Extract, check, decide on an arbitrary draft; used by the error runs."""
    from balancecheck.checks.checks import check_all
    from balancecheck.drafting.surface import extract_claims
    from balancecheck.gate.policy import decide

    claims = extract_claims(draft, ledger, claim_prefix=tag)
    check_all(claims, ledger, client, stub_key_prefix=f"errors:{tag}")
    decision = decide(claims, ledger, revision_index=0, draft_hashes=[])
    append_event(
        VerificationEvent(gen_id=tag, claims=claims, decision=decision),
        cfg.log_path,
    )
    failed = [
        c.check_result.status.value
        for c in claims
        if c.check_result and c.check_result.status.value != "pass"
    ]
    return {
        "decision_action": decision.action.value,
        "decision_reason": decision.reason,
        "failed_statuses": failed,
        "claims_total": len(claims),
    }


def cmd_errors(_args) -> int:
    from balancecheck.bench.corrupt import (
        break_sum,
        fabricate_id,
        inject_fuzzy,
        swap_amount,
        tier2_instructions,
    )
    from balancecheck.drafting.drafter import build_draft_prompt
    from balancecheck.drafting.golden import golden_draft
    from balancecheck.runner import run_scenario

    cfg = load_config()
    run_id = new_run_id("errors")
    _emit_run_start(cfg, run_id, "errors", "A", "empty")
    client = _client(cfg, run_id)
    rows: list[dict] = []

    # Clean controls: golden drafts must verify clean; a verifier that flags
    # them is a rubber stamp in reverse, and balanced accuracy needs the
    # negative class.
    for sid in ("S-A-01", "S-A-02", "S-A-03", "S-A-04", "S-A-05"):
        ledger = _load_ledger(sid)
        draft = golden_draft(ledger)
        res = _verify_draft(cfg, client, ledger, draft, f"clean-{sid}")
        rows.append(
            {
                "error_id": f"E-CLEAN-{sid}",
                "tier": 0,
                "scenario_id": sid,
                "truth": "clean",
                "expected_kind": "none",
                "expected_action": "human_gate",
                "draft": draft,
                **res,
                "caught": res["decision_action"] != "human_gate",
            }
        )

    # Tier 1: programmatic corruptions of a known-good draft.
    tier1 = [
        ("E-T1-01", swap_amount, "revise_or_escalate"),
        ("E-T1-02", fabricate_id, "escalate"),
        ("E-T1-03", break_sum, "revise_or_escalate"),
    ]
    for error_id, fn, expected_action in tier1:
        for sid in ("S-A-01", "S-A-02"):
            ledger = _load_ledger(sid)
            corrupted = fn(golden_draft(ledger), ledger)
            if corrupted is None:
                continue
            res = _verify_draft(cfg, client, ledger, corrupted.draft, f"{error_id}-{sid}")
            caught = res["decision_action"] in ("revise", "escalate", "abstain")
            rows.append(
                {
                    "error_id": f"{error_id}-{sid}",
                    "tier": 1,
                    "scenario_id": sid,
                    "truth": "corrupted",
                    "expected_kind": corrupted.expected_kind,
                    "expected_action": expected_action,
                    "draft": corrupted.draft,
                    **res,
                    "caught": caught,
                }
            )

    # Tier 2: prompted misreadings, live only (they require a real drafter
    # reasoning badly over correct records). Each naive instruction runs
    # against the scenario whose deliberate structure it attacks; running
    # a credit-memo misreading against a ledger with no credit memos proves
    # nothing (a lesson from the first live run of this harness).
    tier2_scenarios = {
        "T2-SUM-IGNORES-UNAPPLIED": "S-A-02",   # unapplied cash
        "T2-FULL-ORIGINAL-AMOUNTS": "S-A-03",   # partial payment
        "T2-CREDITS-ASSUMED-APPLIED": "S-A-04", # open credit memo
    }
    if cfg.mode == "live":
        for error_id, instruction in tier2_instructions():
            ledger = _load_ledger(tier2_scenarios.get(error_id, "S-A-02"))
            system, user = build_draft_prompt(ledger, [], correction="")
            resp = client.complete(
                "draft",
                user + "\n\nIMPORTANT OVERRIDE: " + instruction,
                system=system,
                meta={"error_id": error_id},
            )
            if not resp.ok:
                rows.append(
                    {
                        "error_id": error_id,
                        "tier": 2,
                        "scenario_id": ledger.scenario_id,
                        "truth": "corrupted",
                        "expected_kind": "c_sum_or_amt_fail",
                        "expected_action": "revise_or_escalate",
                        "draft": "",
                        "decision_action": "model_error",
                        "decision_reason": resp.error,
                        "failed_statuses": [],
                        "claims_total": 0,
                        "caught": False,
                    }
                )
                continue
            res = _verify_draft(cfg, client, ledger, resp.text, f"{error_id}")
            caught = res["decision_action"] in ("revise", "escalate", "abstain")
            rows.append(
                {
                    "error_id": error_id,
                    "tier": 2,
                    "scenario_id": ledger.scenario_id,
                    "truth": "corrupted",
                    "expected_kind": "c_sum_or_amt_fail",
                    "expected_action": "revise_or_escalate",
                    "draft": resp.text,
                    **res,
                    "caught": caught,
                }
            )
    else:
        print("stub mode: Tier 2 (prompted misreadings) skipped, requires a live drafter")

    # Tier 3: the ambiguous-allocation scenario; only abstain or escalate is
    # a catch, any confident draft is a miss.
    ledger = _load_ledger("S-A-06")
    outcome = run_scenario(
        cfg, client, ledger, pass_label="errors", run_id=run_id, stub_prefix="stub"
    )
    rows.append(
        {
            "error_id": "E-T3-01",
            "tier": 3,
            "scenario_id": "S-A-06",
            "truth": "ambiguous",
            "expected_kind": "ambiguous_allocation",
            "expected_action": "abstain_or_escalate",
            "draft": outcome.final_draft,
            "decision_action": outcome.terminal_action,
            "decision_reason": outcome.reason,
            "failed_statuses": [],
            "claims_total": 0,
            "caught": outcome.terminal_action in ("abstain", "escalate"),
        }
    )

    # Tier 4: fuzzy unsupported claims, aimed at the model verifier's
    # false-negative rate; live only.
    if cfg.mode == "live":
        for sid in ("S-A-01", "S-A-03"):
            ledger = _load_ledger(sid)
            corrupted = inject_fuzzy(golden_draft(ledger))
            res = _verify_draft(cfg, client, ledger, corrupted.draft, f"E-T4-{sid}")
            caught = res["decision_action"] in ("revise", "escalate") and any(
                s in ("unsupported", "cannot_determine") for s in res["failed_statuses"]
            )
            rows.append(
                {
                    "error_id": f"E-T4-{sid}",
                    "tier": 4,
                    "scenario_id": sid,
                    "truth": "corrupted",
                    "expected_kind": corrupted.expected_kind,
                    "expected_action": corrupted.expected_action,
                    "draft": corrupted.draft,
                    **res,
                    "caught": caught,
                }
            )
    else:
        print("stub mode: Tier 4 (fuzzy claims) skipped, requires the live verifier")

    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / "injected_errors.json"
    out.write_text(json.dumps(rows, indent=2) + "\n")
    caught = sum(1 for r in rows if r["truth"] != "clean" and r["caught"])
    total = sum(1 for r in rows if r["truth"] != "clean")
    clean_ok = sum(1 for r in rows if r["truth"] == "clean" and not r["caught"])
    clean_total = sum(1 for r in rows if r["truth"] == "clean")
    print(f"corrupted caught: {caught}/{total}; clean passed: {clean_ok}/{clean_total}")
    print(f"raw rows written to {out}")
    return 0


# ---------------------------------------------------------------------------
# evidence: calibration bench
# ---------------------------------------------------------------------------


def cmd_bench_select(_args) -> int:
    cfg = load_config()
    rng = random.Random(cfg.seed)
    errors_file = RAW / "injected_errors.json"
    if not errors_file.exists():
        print("run `make errors` first: the calibration set draws from its drafts")
        return 1
    rows = json.loads(errors_file.read_text())
    clean = [r for r in rows if r["truth"] == "clean" and r["draft"]]
    corrupted = [r for r in rows if r["truth"] in ("corrupted",) and r["draft"]]
    events = read_events(cfg.log_path)
    live_finals = [
        (e.scenario_id, e.draft)
        for e in events
        if isinstance(e, GenerationEvent) and e.pass_label in ("pass1", "pass2") and e.draft
    ]
    pool_clean = [
        {"scenario_id": sid, "draft": d, "source": "live_pass"} for sid, d in live_finals
    ]
    pool_clean += [
        {"scenario_id": r["scenario_id"], "draft": r["draft"], "source": r["error_id"]}
        for r in clean
    ]
    pool_bad = [
        {"scenario_id": r["scenario_id"], "draft": r["draft"], "source": r["error_id"]}
        for r in corrupted
    ]
    rng.shuffle(pool_clean)
    rng.shuffle(pool_bad)
    take_clean, take_bad = pool_clean[:8], pool_bad[:8]
    items = take_clean + take_bad
    rng.shuffle(items)
    LABELS.mkdir(exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    blind, truth = [], {}
    for n, item in enumerate(items, 1):
        item_id = f"C-{n:02d}"
        blind.append(
            {"item_id": item_id, "scenario_id": item["scenario_id"], "draft": item["draft"]}
        )
        truth[item_id] = {"source": item["source"]}
    (LABELS / "calibration_set.json").write_text(json.dumps(blind, indent=2) + "\n")
    (RAW / "calibration_truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    template = "item_id,acceptable,tone,clarity\n" + "".join(
        f"{b['item_id']},,,\n" for b in blind
    )
    (LABELS / "labels_template.csv").write_text(template)
    print(
        f"blind calibration set of {len(blind)} drafts written to labels/calibration_set.json; "
        "label it into labels/labels.csv BEFORE running bench-judge. The truth mapping is "
        "sealed in results/raw/calibration_truth.json; do not open it until labels.csv is committed."
    )
    return 0


def cmd_bench_judge(_args) -> int:
    from balancecheck.bench.judge import judge_draft
    from balancecheck.drafting.drafter import render_ledger_block

    cfg = load_config()
    run_id = new_run_id("bench")
    _emit_run_start(cfg, run_id, "bench", "-", "empty")
    client = _client(cfg, run_id)
    items = json.loads((LABELS / "calibration_set.json").read_text())
    scores = {}
    for item in items:
        ledger = _load_ledger(item["scenario_id"])
        result = judge_draft(
            client,
            cfg,
            gen_id=item["item_id"],
            draft=item["draft"],
            ledger_block=render_ledger_block(ledger),
            stub_key=f"bench:{item['item_id']}",
            run_id=run_id,
        )
        scores[item["item_id"]] = result
        ok = "ok" if result["parse_ok"] else "PARSE FAIL"
        print(f"{item['item_id']}: {ok}")
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "judge_scores.json").write_text(json.dumps(scores, indent=2) + "\n")
    print(f"judge scores written to {RAW / 'judge_scores.json'}")
    return 0


def cmd_bench_agree(_args) -> int:
    from balancecheck.bench.calibrate import agreement_report

    labels_file = LABELS / "labels.csv"
    if not labels_file.exists():
        print("labels/labels.csv not found; label the blind set first")
        return 1
    human: dict[str, dict] = {}
    for line in labels_file.read_text().strip().splitlines()[1:]:
        item_id, acceptable, tone, clarity = [p.strip() for p in line.split(",")]
        human[item_id] = {
            "acceptable": acceptable.lower() in ("1", "true", "yes", "y"),
            "tone": int(tone),
            "clarity": int(clarity),
        }
    judge_rows = json.loads((RAW / "judge_scores.json").read_text())
    judge: dict[str, dict] = {}
    for item_id, row in judge_rows.items():
        s = row.get("scores") or {}
        if row.get("parse_ok") and s:
            judge[item_id] = {
                "acceptable": bool(s["acceptable"]),
                "tone": int(s["tone"]),
                "clarity": int(s["clarity"]),
            }
    report = agreement_report(human, judge)
    truth = json.loads((RAW / "calibration_truth.json").read_text())
    for subset_name, pred in (
        ("clean_subset", lambda s: "CLEAN" in s or "live_pass" in s),
        ("corrupted_subset", lambda s: not ("CLEAN" in s or "live_pass" in s)),
    ):
        ids = [i for i in human if i in judge and pred(truth[i]["source"])]
        if len(ids) >= 4:
            sub = agreement_report(
                {i: human[i] for i in ids}, {i: judge[i] for i in ids}
            )
            report[subset_name] = {"n": len(ids), "binary_kappa": sub.get("binary_kappa")}
    report["n_labeled"] = len(human)
    report["n_judged"] = len(judge)
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "calibration.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


def cmd_bench_pairwise(_args) -> int:
    from balancecheck.bench.pairwise import pairwise_both_orders
    from balancecheck.drafting.drafter import render_ledger_block

    cfg = load_config()
    run_id = new_run_id("pairwise")
    client = _client(cfg, run_id)
    events = read_events(cfg.log_path)
    firsts: dict[tuple[str, str], str] = {}
    for e in events:
        if isinstance(e, GenerationEvent) and e.is_first_pass:
            if e.pass_label in ("pass1", "pass2"):
                firsts[(e.pass_label, e.scenario_id)] = e.draft
    rows = []
    scenarios = sorted({sid for (_, sid) in firsts})
    for sid in scenarios:
        a = firsts.get(("pass1", sid))
        b = firsts.get(("pass2", sid))
        if not a or not b:
            continue
        ledger = _load_ledger(sid)
        result = pairwise_both_orders(
            client,
            cfg,
            pair_id=f"pair-{sid}",
            draft_a=a,
            draft_b=b,
            ledger_block=render_ledger_block(ledger),
            stub_key_prefix=f"pairwise:{sid}",
            run_id=run_id,
        )
        rows.append({"scenario_id": sid, **result})
        print(f"{sid}: verdict={result['verdict']} flipped={result['flipped']}")
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "pairwise.json").write_text(json.dumps(rows, indent=2) + "\n")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def cmd_report(_args) -> int:
    from balancecheck.spine.report import write_all

    cfg = load_config()
    RESULTS.mkdir(exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    write_all(cfg, RESULTS, RAW)
    print(f"results written to {RESULTS}")
    return 0


def cmd_readme_inject(_args) -> int:
    from balancecheck.spine.report import readme_inject

    readme = REPO_ROOT / "README.md"
    new_text = readme_inject(readme, RESULTS)
    readme.write_text(new_text)
    print("README numbers regenerated from results/")
    return 0


def cmd_verify_report_determinism(_args) -> int:
    import tempfile

    from balancecheck.spine.report import write_all

    cfg = load_config()
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        write_all(cfg, Path(d1), RAW)
        write_all(cfg, Path(d2), RAW)
        for f1 in sorted(Path(d1).glob("*.md")):
            f2 = Path(d2) / f1.name
            if f1.read_bytes() != f2.read_bytes():
                print(f"NONDETERMINISTIC: {f1.name}")
                return 1
    print("report generation is deterministic (invariant I10)")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="balancecheck")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("make-stubs")
    sub.add_parser("demo")

    p = sub.add_parser("run-pool")
    p.add_argument("--pool", required=True, choices=["A", "B"])
    p.add_argument("--pass-label", required=True)
    p.add_argument("--with-memory", action="store_true")
    p.add_argument("--with-random-memory", action="store_true")

    p = sub.add_parser("review")
    p.add_argument("--list", action="store_true")
    p.add_argument("--apply", metavar="DECISIONS_JSON")

    sub.add_parser("ingest")
    sub.add_parser("errors")
    sub.add_parser("bench-select")
    sub.add_parser("bench-judge")
    sub.add_parser("bench-agree")
    sub.add_parser("bench-pairwise")
    sub.add_parser("report")
    sub.add_parser("readme-inject")
    sub.add_parser("verify-report-determinism")

    args = parser.parse_args(argv)
    handlers = {
        "make-stubs": cmd_make_stubs,
        "demo": cmd_demo,
        "run-pool": cmd_run_pool,
        "review": cmd_review,
        "ingest": cmd_ingest,
        "errors": cmd_errors,
        "bench-select": cmd_bench_select,
        "bench-judge": cmd_bench_judge,
        "bench-agree": cmd_bench_agree,
        "bench-pairwise": cmd_bench_pairwise,
        "report": cmd_report,
        "readme-inject": cmd_readme_inject,
        "verify-report-determinism": cmd_verify_report_determinism,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
