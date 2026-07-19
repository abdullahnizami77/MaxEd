"""Regression tests for the fuzzy-citation requirement (G7) and the
review-apply guard with edited-text re-verification (G8)."""

from __future__ import annotations

import json

import pytest

from balancecheck.checks.checks import check_fuzzy
from balancecheck.config import Config
from balancecheck.contracts.models import CheckStatus, Claim, ClaimType
from balancecheck.model_client import ModelClient
from balancecheck.substrate.foundry import build_all

LEDGER = build_all()[0]  # S-A-01


def _client(tmp_path, verdict: dict) -> ModelClient:
    stub = {f"t:fuzzy:f-1": {"text": json.dumps(verdict)}}
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps(stub))
    cfg = Config(mode="stub", log_path=tmp_path / "ev.jsonl", stub_file=stub_file)
    return ModelClient(cfg=cfg, run_id="t")


def _claim() -> Claim:
    return Claim(
        claim_id="f-1",
        type=ClaimType.C_FUZZY,
        span="Your account manager approved a waiver of the late fees.",
        token="",
    )


def test_supported_without_citation_does_not_pass(tmp_path):
    client = _client(tmp_path, {"verdict": "supported", "cited_record_ids": [], "reason": "seems fine"})
    result = check_fuzzy(_claim(), LEDGER, client, stub_key_prefix="t")
    assert result.status is CheckStatus.CANNOT_DETERMINE
    assert "cited no record" in result.detail


def test_supported_with_valid_citation_passes(tmp_path):
    real_id = LEDGER.invoices[0].id
    client = _client(
        tmp_path, {"verdict": "supported", "cited_record_ids": [real_id], "reason": "matches"}
    )
    result = check_fuzzy(_claim(), LEDGER, client, stub_key_prefix="t")
    assert result.status is CheckStatus.PASS
    assert real_id in result.cited_records


def test_unsupported_with_no_citation_states_absence(tmp_path):
    client = _client(tmp_path, {"verdict": "unsupported", "cited_record_ids": [], "reason": ""})
    result = check_fuzzy(_claim(), LEDGER, client, stub_key_prefix="t")
    assert result.status is CheckStatus.UNSUPPORTED
    assert "no record" in result.detail.lower()  # honest absence, not a fabricated contradiction


# --- G8: review-apply guard + re-verification of edited text ---------------


def _decisions_file(tmp_path, rows) -> str:
    p = tmp_path / "decisions.json"
    p.write_text(json.dumps(rows))
    return str(p)


def test_review_apply_refuses_non_gated_generation(tmp_path, monkeypatch, capsys):
    # A gen_id that exists but never reached the human gate must be skipped
    # unless overridden. Driven through the CLI with a temp log.
    from balancecheck import cli
    from balancecheck.contracts.models import (
        GenerationEvent,
        VerificationEvent,
        GateDecision,
        GateAction,
    )
    from balancecheck.spine.events import append_event

    log = tmp_path / "ev.jsonl"
    monkeypatch.setattr(cli, "load_config", lambda **k: Config(mode="stub", log_path=log, stub_file=tmp_path / "s.json"))
    # a generation that ESCALATED (not human_gate)
    append_event(GenerationEvent(gen_id="g1", scenario_id="S-A-01", pool="A", pass_label="poolA",
                                 ledger_ref="x", prompt_sha="y", draft="INV-2943 is overdue.",
                                 revision_index=0, is_first_pass=True), log)
    append_event(VerificationEvent(gen_id="g1", claims=[],
                                   decision=GateDecision(action=GateAction.ESCALATE, reason="x")), log)
    args = type("A", (), {"list": False, "apply": _decisions_file(tmp_path, [{"gen_id": "g1", "action": "approve"}])})()
    cli.cmd_review(args)
    out = capsys.readouterr().out
    assert "not awaiting a human decision" in out
    assert "applied 0" in out
