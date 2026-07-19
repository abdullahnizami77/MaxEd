"""Invariant I12: the harness grounding oracle is independent of the gate.

Proves three things. First, by AST scan, oracle.py imports only the standard
library (json, re, pathlib) and never balancecheck.checks,
balancecheck.substrate.derive, or balancecheck.drafting. Second, over a raw
fixture written to tmp_path, the oracle's own replay verifies a correct
draft completely and lands a wrong amount in unmatched. Third,
cross-agreement: the checks-side path (when importable) and the oracle agree
that the same correct draft is fully grounded, and the two independently
authored renderers and parsers agree on sample values.

Hermetic: the fixture is a hand-written dict in tmp_path; no foundry, no
network, no model.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from balancecheck.bench import oracle

ORACLE_PATH = Path(oracle.__file__)

FORBIDDEN_PREFIXES = (
    "balancecheck.checks",
    "balancecheck.substrate.derive",
    "balancecheck.drafting",
)
ALLOWED_MODULES = {"__future__", "json", "re", "pathlib"}


def _imported_modules() -> set[str]:
    tree = ast.parse(ORACLE_PATH.read_text(encoding="utf-8"), filename=str(ORACLE_PATH))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


def test_oracle_never_imports_the_gate_side() -> None:
    modules = _imported_modules()
    for module in modules:
        assert not module.startswith(FORBIDDEN_PREFIXES), (
            f"oracle.py imports forbidden module {module}"
        )
        assert not module.startswith("balancecheck"), (
            f"oracle.py must be stdlib-only but imports {module}"
        )


def test_oracle_imports_are_the_declared_stdlib_set() -> None:
    assert _imported_modules() <= ALLOWED_MODULES


# ---------------------------------------------------------------------------
# The oracle's own math over a raw fixture
# ---------------------------------------------------------------------------

# INV-1001 240000 with 50000 applied from PMT-2001 (open 190000);
# INV-1002 76000 open; net balance 266000.
FIXTURE = {
    "schema_version": 1,
    "scenario_id": "S-T-90",
    "pool": "B",
    "structure_labels": [],
    "client": {"id": "CL-T", "name": "Test Client", "terms_days": 30, "as_of": "2026-03-31"},
    "invoices": [
        {"id": "INV-1001", "date": "2026-02-01", "due": "2026-03-03", "amount_cents": 240000, "memo": ""},
        {"id": "INV-1002", "date": "2026-03-01", "due": "2026-03-31", "amount_cents": 76000, "memo": ""},
    ],
    "payments": [
        {"id": "PMT-2001", "date": "2026-03-10", "amount_cents": 50000, "method": "ach", "reference": ""},
    ],
    "credit_memos": [],
    "applications": [
        {"source_id": "PMT-2001", "target_invoice": "INV-1001", "amount_cents": 50000},
    ],
}

CORRECT_DRAFT = (
    "As of 2026-03-31 the balance due is $2,660.00. Invoice INV-1001 has an"
    " open amount of $1,900.00 and invoice INV-1002 remains open at $760.00."
    " Your payment PMT-2001 of $500.00 was applied."
)


def _write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "S-T-90.json"
    path.write_text(json.dumps(FIXTURE, indent=2) + "\n", encoding="utf-8")
    return path


def test_oracle_verifies_a_correct_draft_completely(tmp_path: Path) -> None:
    fixture_path = _write_fixture(tmp_path)
    result = oracle.oracle_grounding(CORRECT_DRAFT, fixture_path)
    # 4 dollar amounts + 3 document IDs.
    assert result["total"] == 7
    assert result["verified"] == result["total"]
    assert result["unmatched"] == []


def test_oracle_lands_a_wrong_amount_in_unmatched(tmp_path: Path) -> None:
    fixture_path = _write_fixture(tmp_path)
    wrong = CORRECT_DRAFT + " A processing fee of $12.34 applies."
    result = oracle.oracle_grounding(wrong, fixture_path)
    assert result["total"] == 8
    assert result["verified"] == 7
    assert result["unmatched"] == [{"token": "$12.34", "kind": "amount"}]


def test_oracle_lands_a_fabricated_id_in_unmatched(tmp_path: Path) -> None:
    fixture_path = _write_fixture(tmp_path)
    wrong = CORRECT_DRAFT + " Please also settle INV-9999."
    result = oracle.oracle_grounding(wrong, fixture_path)
    assert {"token": "INV-9999", "kind": "id"} in result["unmatched"]
    assert result["verified"] == result["total"] - 1


def test_oracle_replay_math_is_correct_by_hand() -> None:
    truth, ids = oracle.truth_sets(FIXTURE)
    # Hand arithmetic, independent of both derive.py and the oracle's replay:
    # net = (240000 - 50000) + 76000 = 266000.
    assert 266000 in truth        # net balance and open invoice total
    assert 190000 in truth        # open amount of INV-1001
    assert 76000 in truth         # INV-1002 amount and open amount
    assert 240000 in truth        # INV-1001 original amount
    assert 50000 in truth         # payment amount
    # Zero is deliberately absent when money is owed: a fabricated "$0.00"
    # claim must not verify (adversarial-review hardening).
    assert 0 not in truth
    assert ids == {"INV-1001", "INV-1002", "PMT-2001"}


# ---------------------------------------------------------------------------
# Cross-agreement with the gate side (the full battery runs in integration)
# ---------------------------------------------------------------------------


def test_cross_agreement_on_a_correct_draft(tmp_path: Path) -> None:
    fixture_path = _write_fixture(tmp_path)
    oracle_result = oracle.oracle_grounding(CORRECT_DRAFT, fixture_path)
    assert oracle_result["verified"] == oracle_result["total"]

    try:
        from balancecheck.checks.checks import run_code_checks
        from balancecheck.contracts.models import (
            CheckStatus,
            Claim,
            ClaimType,
            Ledger,
        )
    except ImportError:
        # Checks side not importable in this build: the oracle's own math
        # above already carries the assertion.
        return

    from balancecheck.bench.score import grounding

    ledger = Ledger.model_validate(FIXTURE)
    claims = [
        Claim(claim_id="x1", type=ClaimType.C_SUM, span="the balance due is $2,660.00", token="$2,660.00"),
        Claim(
            claim_id="x2",
            type=ClaimType.C_AMT,
            span="INV-1001 has an open amount of $1,900.00",
            token="$1,900.00",
            subject_id="INV-1001",
        ),
        Claim(claim_id="x3", type=ClaimType.C_EXIST, span="invoice INV-1002", token="INV-1002"),
        Claim(claim_id="x4", type=ClaimType.C_EXIST, span="payment PMT-2001", token="PMT-2001"),
    ]
    run_code_checks(claims, ledger)
    passed, total = grounding(claims)
    assert total == 4
    assert passed == total, [
        (c.claim_id, c.check_result.status if c.check_result else None) for c in claims
    ]
    # Both independently authored paths call the same correct draft fully
    # grounded; a divergence here would be a checker or oracle bug surfacing.
    assert (passed == total) == (oracle_result["verified"] == oracle_result["total"])
    for claim in claims:
        assert claim.check_result is not None
        assert claim.check_result.status is CheckStatus.PASS


def test_independent_renderers_and_parsers_agree() -> None:
    from balancecheck.substrate.money import cents, parse_dollars, render

    for value in (0, 5, 50, 76000, 190000, 240000, 266000, 123456789, -15000):
        assert oracle.render_cents(value) == render(cents(value))
        assert oracle.parse_cents(render(cents(value))) == value
        assert parse_dollars(oracle.render_cents(value)) == value
