"""Invariant I4: the fuzzy verifier sees one claim sentence and the records,
never the surrounding draft, never sibling claims.

The draft here carries a distinctive marker string that appears nowhere in
the ledger; if any assembly path smuggled draft context into the verifier
prompt, the marker or a sibling sentence would surface and these assertions
would fail. The trace-log check proves the property on the prompt actually
sent, not just on the builder's return value.
"""

from __future__ import annotations

import json
from datetime import date

from balancecheck.checks.checks import build_fuzzy_prompt, check_all, render_records_block
from balancecheck.config import Config
from balancecheck.contracts.models import (
    Application,
    Claim,
    ClaimType,
    ClientInfo,
    Invoice,
    Ledger,
    Payment,
    TraceEvent,
)
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import read_events

MARKER = "XK-DRAFT-ONLY-MARKER-9931"

SENTENCE_ONE = f"The balance due on your account is $1,200.00 per ref {MARKER}."
SENTENCE_TWO = "Your account manager approved a goodwill adjustment on this account."
SENTENCE_THREE = "Invoice INV-1012 remains open at $1,200.00 as of 2026-03-31."

DRAFT = " ".join([SENTENCE_ONE, SENTENCE_TWO, SENTENCE_THREE])


def make_ledger() -> Ledger:
    return Ledger(
        scenario_id="T-ISO",
        pool="A",
        structure_labels=[],
        client=ClientInfo(
            id="CL-T", name="Test Client Co.", terms_days=30, as_of=date(2026, 3, 31)
        ),
        invoices=[
            Invoice(
                id="INV-1012",
                date=date(2026, 3, 1),
                due=date(2026, 3, 31),
                amount_cents=120000,
                memo="design retainer",
            ),
            Invoice(
                id="INV-1013",
                date=date(2026, 2, 1),
                due=date(2026, 3, 3),
                amount_cents=60000,
                memo="print run",
            ),
        ],
        payments=[
            Payment(
                id="PMT-0208",
                date=date(2026, 3, 5),
                amount_cents=60000,
                method="ach",
                reference="ACH-88213",
            )
        ],
        credit_memos=[],
        applications=[
            Application(
                source_id="PMT-0208", target_invoice="INV-1013", amount_cents=60000
            )
        ],
    )


def make_claims() -> list[Claim]:
    """Hand-built claims: one per sentence, the fuzzy one in the middle."""
    return [
        Claim(
            claim_id="i-001",
            type=ClaimType.C_SUM,
            span=SENTENCE_ONE,
            token="$1,200.00",
        ),
        Claim(
            claim_id="i-002",
            type=ClaimType.C_FUZZY,
            span=SENTENCE_TWO,
            token="",
        ),
        Claim(
            claim_id="i-003",
            type=ClaimType.C_AMT,
            span=SENTENCE_THREE,
            token="$1,200.00",
            subject_id="INV-1012",
        ),
    ]


def test_marker_is_really_draft_only():
    """The isolation assertions below are only meaningful if the marker and
    the sibling sentences cannot enter the prompt via the ledger itself."""
    records = render_records_block(make_ledger())
    assert MARKER not in records
    assert SENTENCE_ONE not in records
    assert SENTENCE_THREE not in records


def test_fuzzy_prompt_contains_the_claim_and_nothing_of_the_draft():
    ledger = make_ledger()
    claims = make_claims()
    fuzzy = claims[1]
    system, user = build_fuzzy_prompt(fuzzy, ledger)

    # The one claim sentence is present.
    assert fuzzy.span in user

    # The other draft sentences are absent, from both prompt halves.
    for sibling in (claims[0], claims[2]):
        assert sibling.span not in user
        assert sibling.span not in system

    # The distinctive draft-only marker is absent.
    assert MARKER not in user
    assert MARKER not in system

    # The full draft blob is absent.
    assert DRAFT not in user

    # The ledger slice is present: the verifier judges against records.
    assert "INV-1012" in user
    assert "2026-03-31" in user


def test_prompt_actually_sent_to_the_client_is_isolated(tmp_path):
    """Read the trace line for the verify_fuzzy call and assert isolation on
    the exact prompt that went out the door."""
    ledger = make_ledger()
    claims = make_claims()
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(
        json.dumps(
            {
                "iso:fuzzy:i-002": {
                    "text": json.dumps(
                        {
                            "verdict": "unsupported",
                            "cited_record_ids": [],
                            "reason": "no approval in the records",
                        }
                    )
                }
            }
        )
    )
    cfg = Config(mode="stub", log_path=tmp_path / "events.jsonl", stub_file=stub_file)
    client = ModelClient(cfg=cfg, run_id="t")
    check_all(claims, ledger, client, stub_key_prefix="iso")

    traces = [
        e
        for e in read_events(cfg.log_path)
        if isinstance(e, TraceEvent) and e.task == "verify_fuzzy"
    ]
    assert len(traces) == 1, "exactly one fuzzy verification call was made"
    sent = traces[0].prompt
    assert SENTENCE_TWO in sent
    assert MARKER not in sent
    assert SENTENCE_ONE not in sent
    assert SENTENCE_THREE not in sent
