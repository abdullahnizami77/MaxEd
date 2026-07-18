"""Drafter tests: prompt assembly, block collapse, and stub-mode generation.

Hermetic: the ledger is constructed inline, the stub responses file and the
event log live in pytest tmp_path, and no network or credentials are used.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from balancecheck.config import Config
from balancecheck.contracts.models import (
    Application,
    ClientInfo,
    CreditMemo,
    HumanAction,
    Invoice,
    Ledger,
    MemoryEntry,
    Payment,
    STRUCTURE_LABELS,
    TraceEvent,
)
from balancecheck.drafting.drafter import (
    DRAFT_TEMPLATE_PATH,
    DraftResult,
    build_draft_prompt,
    generate_draft,
    render_ledger_block,
)
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import read_events

EM_DASH = "\u2014"


def _ledger() -> Ledger:
    return Ledger(
        scenario_id="S-T-01",
        pool="A",
        client=ClientInfo(id="CL-T", name="Test Client LLC", terms_days=30, as_of=date(2026, 3, 31)),
        invoices=[
            Invoice(id="INV-1012", date=date(2026, 2, 1), due=date(2026, 3, 3), amount_cents=240000),
            Invoice(id="INV-1020", date=date(2026, 3, 1), due=date(2026, 3, 31), amount_cents=150000),
        ],
        payments=[Payment(id="PMT-0208", date=date(2026, 3, 10), amount_cents=100000)],
        credit_memos=[CreditMemo(id="CM-0031", date=date(2026, 3, 15), amount_cents=15000)],
        applications=[
            Application(source_id="PMT-0208", target_invoice="INV-1012", amount_cents=100000)
        ],
    )


def _exemplar(text: str, n: int = 1) -> MemoryEntry:
    return MemoryEntry(
        entry_id=f"m-{n:03d}",
        source_gen_id=f"g-{n:03d}",
        scenario_id="S-A-01",
        pool="A",
        signature={label: False for label in STRUCTURE_LABELS},
        weight=2,
        final_text=text,
        human_action=HumanAction.EDIT,
        ingested_offset=0,
    )


# ---------------------------------------------------------------------------
# Ledger block and prompt assembly
# ---------------------------------------------------------------------------


def test_ledger_block_carries_every_computed_number() -> None:
    block = render_ledger_block(_ledger())
    assert "NET BALANCE DUE: $2,750.00" in block
    assert "open invoice total: $2,900.00" in block
    assert "unapplied cash total: $0.00" in block
    assert "unapplied credit total: $150.00" in block
    assert "amount $2,400.00" in block
    assert "open $1,400.00" in block
    assert "overdue" in block
    assert "PMT-0208 -> INV-1012: $1,000.00" in block


def test_prompt_contains_net_balance_and_open_invoice_ids() -> None:
    system, user = build_draft_prompt(_ledger(), [])
    assert "NET BALANCE DUE: $2,750.00" in user
    assert "INV-1012" in user
    assert "INV-1020" in user
    assert "$X,XXX.XX" in system
    assert "Accounts Receivable" in system


def test_exemplars_block_present_when_given() -> None:
    exemplar_text = "Dear client, your balance due is $9,999.00. Accounts Receivable"
    _, user = build_draft_prompt(_ledger(), [_exemplar(exemplar_text)])
    assert "Examples of replies our reviewers approved for similar accounts:" in user
    assert exemplar_text in user


def test_exemplars_block_vanishes_when_empty() -> None:
    _, user = build_draft_prompt(_ledger(), [])
    assert "Examples of replies our reviewers approved" not in user
    assert "{{" not in user
    assert "\n\n\n" not in user


def test_correction_block_appears_under_revision_required() -> None:
    correction = "C-SUM failed: the ledger shows $2,750.00, you wrote $2,900.00."
    _, user = build_draft_prompt(_ledger(), [], correction)
    assert "REVISION REQUIRED. Your previous draft failed verification:" in user
    assert correction in user
    assert "Fix ONLY those errors" in user


def test_correction_block_vanishes_when_empty() -> None:
    _, user = build_draft_prompt(_ledger(), [])
    assert "REVISION REQUIRED" not in user


def test_template_and_prompts_contain_no_em_dash() -> None:
    assert EM_DASH not in DRAFT_TEMPLATE_PATH.read_text(encoding="utf-8")
    system, user = build_draft_prompt(
        _ledger(), [_exemplar("An exemplar reply.")], "A correction note."
    )
    assert EM_DASH not in system
    assert EM_DASH not in user


# ---------------------------------------------------------------------------
# Stub-mode generation with trace discipline
# ---------------------------------------------------------------------------

CANNED_DRAFT = (
    "Dear Test Client LLC,\n"
    "\n"
    "As of 2026-03-31, our records show a net balance due of $2,750.00.\n"
    "\n"
    "Open invoices:\n"
    "INV-1012, open $1,400.00, due 2026-03-03.\n"
    "INV-1020, open $1,500.00, due 2026-03-31.\n"
    "\n"
    "An unapplied credit memo CM-0031 of $150.00 reduces the amount owed.\n"
    "\n"
    "Thank you,\n"
    "Accounts Receivable"
)


def test_generate_draft_stub_mode(tmp_path: Path) -> None:
    stub_file = tmp_path / "stub_responses.json"
    stub_file.write_text(json.dumps({"draft-s-t-01": {"text": CANNED_DRAFT}}), encoding="utf-8")
    log_path = tmp_path / "events.jsonl"
    cfg = Config(log_path=log_path, stub_file=stub_file)
    client = ModelClient(cfg=cfg, run_id="test-run")

    result = generate_draft(client, _ledger(), [], stub_key="draft-s-t-01")

    assert isinstance(result, DraftResult)
    assert result.text == CANNED_DRAFT
    assert "NET BALANCE DUE: $2,750.00" in result.prompt
    assert "accounts-receivable assistant" in result.system

    events = read_events(log_path)
    assert len(events) == 1, "exactly one trace line for one stub draft attempt"
    ev = events[0]
    assert isinstance(ev, TraceEvent)
    assert ev.task == "draft"
    assert ev.ok is True
    assert ev.attempt == 1
    assert ev.output == CANNED_DRAFT
    assert ev.prompt == result.prompt
    assert ev.prompt_sha == result.prompt_sha
    assert ev.model_name == "stub"
    assert ev.run_id == "test-run"
    assert ev.meta.get("stub") is True
    assert ev.meta.get("stub_key") == "draft-s-t-01"
