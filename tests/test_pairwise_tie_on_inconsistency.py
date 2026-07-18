"""The pairwise protocol's conservative correction: order-dependent verdicts
collapse to a tie, order-consistent verdicts stand, and each ordering leaves
exactly one JudgmentEvent in the log.

Hermetic: stub client via a temp stub-responses file, temp log path, zero
network, zero credentials. The inconsistent case is constructed so both
orderings answer winner "1" positionally, which means draft a wins in "ab"
and draft b wins in "ba": the orderings DISAGREE about the underlying draft.
"""

from __future__ import annotations

import json
from pathlib import Path

from balancecheck.bench.pairwise import map_winner, pairwise_both_orders
from balancecheck.config import Config
from balancecheck.contracts.models import JudgmentEvent
from balancecheck.model_client import ModelClient
from balancecheck.spine.events import read_events

DRAFT_A = "Dear client, your balance is explained item by item below."
DRAFT_B = "Dear client, please pay the balance."
LEDGER_BLOCK = "CLIENT: Test Co (CL-T) | NET BALANCE DUE: $1,250.00"


def _verdict(winner: str) -> dict:
    return {"text": json.dumps({"winner": winner, "rationale": "stub rationale"})}


def _cfg(tmp_path: Path, stubs: dict) -> Config:
    stub_file = tmp_path / "stubs.json"
    stub_file.write_text(json.dumps(stubs))
    return Config(mode="stub", log_path=tmp_path / "events.jsonl", stub_file=stub_file)


def _judgments(cfg: Config) -> list[JudgmentEvent]:
    return [e for e in read_events(cfg.log_path) if isinstance(e, JudgmentEvent)]


def test_winner_mapping_inverts_with_ordering() -> None:
    assert map_winner("1", "ab") == "a"
    assert map_winner("2", "ab") == "b"
    assert map_winner("1", "ba") == "b"
    assert map_winner("2", "ba") == "a"
    assert map_winner("tie", "ab") == "tie"
    assert map_winner("tie", "ba") == "tie"


def test_inconsistent_orderings_collapse_to_tie(tmp_path: Path) -> None:
    # Positionally the judge says Draft 1 both times; about the underlying
    # drafts that is a wins then b wins: a disagreement.
    cfg = _cfg(tmp_path, {"p1:ab": _verdict("1"), "p1:ba": _verdict("1")})
    client = ModelClient(cfg=cfg, run_id="t")
    result = pairwise_both_orders(
        client, cfg, "p1", DRAFT_A, DRAFT_B, LEDGER_BLOCK, "p1", run_id="t"
    )
    assert result["verdict"] == "tie"
    assert result["flipped"] is True
    assert result["orderings"]["ab"]["verdict"] == "a"
    assert result["orderings"]["ba"]["verdict"] == "b"


def test_consistent_orderings_keep_the_verdict(tmp_path: Path) -> None:
    # Draft a wins as Draft 1 in "ab" and as Draft 2 in "ba": agreement.
    cfg = _cfg(tmp_path, {"p2:ab": _verdict("1"), "p2:ba": _verdict("2")})
    client = ModelClient(cfg=cfg, run_id="t")
    result = pairwise_both_orders(
        client, cfg, "p2", DRAFT_A, DRAFT_B, LEDGER_BLOCK, "p2", run_id="t"
    )
    assert result["verdict"] == "a"
    assert result["flipped"] is False
    assert result["orderings"]["ab"]["verdict"] == "a"
    assert result["orderings"]["ba"]["verdict"] == "a"


def test_consistent_ties_are_a_tie_without_flip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, {"p3:ab": _verdict("tie"), "p3:ba": _verdict("tie")})
    client = ModelClient(cfg=cfg, run_id="t")
    result = pairwise_both_orders(
        client, cfg, "p3", DRAFT_A, DRAFT_B, LEDGER_BLOCK, "p3", run_id="t"
    )
    assert result["verdict"] == "tie"
    assert result["flipped"] is False


def test_exactly_two_judgment_events_per_pair(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        {
            "p1:ab": _verdict("1"),
            "p1:ba": _verdict("1"),
            "p2:ab": _verdict("1"),
            "p2:ba": _verdict("2"),
        },
    )
    client = ModelClient(cfg=cfg, run_id="t")
    pairwise_both_orders(client, cfg, "p1", DRAFT_A, DRAFT_B, LEDGER_BLOCK, "p1", run_id="t")
    pairwise_both_orders(client, cfg, "p2", DRAFT_A, DRAFT_B, LEDGER_BLOCK, "p2", run_id="t")

    judgments = _judgments(cfg)
    by_pair: dict[str, list[JudgmentEvent]] = {}
    for event in judgments:
        by_pair.setdefault(event.pair_id, []).append(event)
    assert set(by_pair) == {"p1", "p2"}
    for pair_id, events in by_pair.items():
        assert len(events) == 2, f"{pair_id}: exactly two JudgmentEvents, one per ordering"
        assert {e.ordering for e in events} == {"ab", "ba"}
        for e in events:
            assert e.mode == "pairwise"
            assert e.parse_ok is True
            assert e.prompt_sha
            assert e.judge_model == "stub"

    # Each ordering's event records that ordering's own mapped verdict.
    p1 = {e.ordering: e.verdict for e in by_pair["p1"]}
    assert p1 == {"ab": "a", "ba": "b"}
    p2 = {e.ordering: e.verdict for e in by_pair["p2"]}
    assert p2 == {"ab": "a", "ba": "a"}


def test_parse_failure_collapses_to_tie_with_event(tmp_path: Path) -> None:
    # The "ba" stub is not valid JSON for the schema: the call fails after
    # retries, the event still lands with parse_ok False, and the pair
    # conservatively resolves to a tie.
    cfg = _cfg(
        tmp_path,
        {"p4:ab": _verdict("1"), "p4:ba": {"text": "no json here"}},
    )
    client = ModelClient(cfg=cfg, run_id="t")
    result = pairwise_both_orders(
        client, cfg, "p4", DRAFT_A, DRAFT_B, LEDGER_BLOCK, "p4", run_id="t"
    )
    assert result["verdict"] == "tie"
    assert result["flipped"] is True
    assert result["orderings"]["ba"]["parse_ok"] is False

    judgments = _judgments(cfg)
    assert len(judgments) == 2, "a failed ordering still appends its JudgmentEvent"
    failed = next(e for e in judgments if e.ordering == "ba")
    assert failed.parse_ok is False
    assert failed.verdict == ""
    assert failed.scores == {}
