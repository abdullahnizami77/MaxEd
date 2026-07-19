"""Tests for the agentic read-only accounting tools (drafting/tools.py).

Hermetic: every ledger comes from substrate.foundry.build_all(), a pure
function of its seed; no filesystem, network, or credentials. Every tool
result is compared against the corresponding derive.py value, every call is
checked to leave the ledger byte-identical, and every failure path must
return the typed {"ok": False, "error": ...} shape instead of raising.
"""

from __future__ import annotations

import pytest

from balancecheck.contracts.models import Ledger
from balancecheck.drafting.tools import (
    TOOL_REGISTRY,
    canonicalize_doc_id,
    execute_tool,
    tool_catalog,
)
from balancecheck.substrate import derive
from balancecheck.substrate.foundry import build_all
from balancecheck.substrate.money import cents, render

# Two structure-bearing fixtures (the partial-payment and open-credit
# scenarios the spec names) plus the unapplied-cash scenario.
SCENARIOS = ("S-A-02", "S-A-03", "S-A-04")

TOOL_NAMES = (
    "get_account_summary",
    "list_open_invoices",
    "get_invoice",
    "list_unapplied_sources",
    "get_source",
    "get_application_history",
)


@pytest.fixture(scope="module")
def ledgers() -> dict[str, Ledger]:
    return {ledger.scenario_id: ledger for ledger in build_all()}


def _run_checked(name: str, arguments: dict, ledger: Ledger) -> dict:
    """Execute one tool call and assert it left the ledger untouched."""
    before = ledger.model_dump()
    result = execute_tool(name, arguments, ledger)
    assert ledger.model_dump() == before, f"{name} mutated the ledger"
    return result


def _data(name: str, arguments: dict, ledger: Ledger) -> dict:
    result = _run_checked(name, arguments, ledger)
    assert result["ok"] is True, result
    return result["data"]


# ---------------------------------------------------------------------------
# Numbers equal derive.py on real fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_account_summary_matches_derive(
    ledgers: dict[str, Ledger], scenario_id: str
) -> None:
    ledger = ledgers[scenario_id]
    data = _data("get_account_summary", {}, ledger)
    assert data["client_id"] == ledger.client.id
    assert data["client_name"] == ledger.client.name
    assert data["as_of"] == ledger.client.as_of.isoformat()
    assert data["open_invoice_total_cents"] == derive.open_invoice_total(ledger)
    assert data["open_invoice_total"] == render(derive.open_invoice_total(ledger))
    assert data["unapplied_cash_total_cents"] == derive.unapplied_cash_total(ledger)
    assert data["unapplied_credit_total_cents"] == derive.unapplied_credit_total(ledger)
    assert data["net_balance_due_cents"] == derive.net_balance(ledger)
    assert data["net_balance_due"] == render(derive.net_balance(ledger))
    assert data["signature"] == derive.signature(ledger)


def test_account_summary_concrete_anchors(ledgers: dict[str, Ledger]) -> None:
    """Non-tautological anchors computed by hand from the foundry parameters."""
    # S-A-04: open 3750.00 + 11200.00, minus the 850.00 open credit.
    data = _data("get_account_summary", {}, ledgers["S-A-04"])
    assert data["open_invoice_total_cents"] == 1495000
    assert data["unapplied_credit_total_cents"] == 85000
    assert data["net_balance_due_cents"] == 1410000
    assert data["net_balance_due"] == "$14,100.00"
    assert data["signature"]["has_open_credit"] is True
    # S-A-03: partial 3000.00 against 8200.00, plus the 1450.50 open invoice.
    data = _data("get_account_summary", {}, ledgers["S-A-03"])
    assert data["open_invoice_total_cents"] == 665050
    assert data["unapplied_cash_total_cents"] == 0
    assert data["net_balance_due_cents"] == 665050
    assert data["signature"]["has_partial"] is True


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_list_open_invoices_matches_derive(
    ledgers: dict[str, Ledger], scenario_id: str
) -> None:
    ledger = ledgers[scenario_id]
    rows = _data("list_open_invoices", {}, ledger)["open_invoices"]
    expected_ids = [
        inv.id for inv in ledger.invoices if derive.open_amount(ledger, inv.id) > 0
    ]
    assert [row["id"] for row in rows] == expected_ids
    for row in rows:
        inv = next(i for i in ledger.invoices if i.id == row["id"])
        assert row["date"] == inv.date.isoformat()
        assert row["due"] == inv.due.isoformat()
        assert row["memo"] == inv.memo
        assert row["original_cents"] == inv.amount_cents
        assert row["original"] == render(cents(inv.amount_cents))
        assert row["applied_cents"] == derive.applied_to_invoice(ledger, inv.id)
        assert row["open_cents"] == derive.open_amount(ledger, inv.id)
        assert row["open"] == render(derive.open_amount(ledger, inv.id))
        assert row["status"] == derive.invoice_status(ledger, inv.id).value


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_get_invoice_matches_derive(
    ledgers: dict[str, Ledger], scenario_id: str
) -> None:
    ledger = ledgers[scenario_id]
    for inv in ledger.invoices:
        data = _data("get_invoice", {"invoice_id": inv.id}, ledger)
        assert data["id"] == inv.id
        assert data["original_cents"] == inv.amount_cents
        assert data["applied_cents"] == derive.applied_to_invoice(ledger, inv.id)
        assert data["applied"] == render(derive.applied_to_invoice(ledger, inv.id))
        assert data["open_cents"] == derive.open_amount(ledger, inv.id)
        assert data["status"] == derive.invoice_status(ledger, inv.id).value
        expected_apps = [
            (a.source_id, a.amount_cents)
            for a in ledger.applications
            if a.target_invoice == inv.id
        ]
        assert [
            (a["source_id"], a["amount_cents"]) for a in data["applications"]
        ] == expected_apps


def test_get_invoice_partial_payment_anchor(ledgers: dict[str, Ledger]) -> None:
    """S-A-03 partial: 3000.00 applied against the 8200.00 signage invoice."""
    data = _data("get_invoice", {"invoice_id": "INV-3063"}, ledgers["S-A-03"])
    assert data["original_cents"] == 820000
    assert data["applied_cents"] == 300000
    assert data["open_cents"] == 520000
    assert data["open"] == "$5,200.00"
    assert data["status"] == "overdue"
    assert data["applications"] == [
        {"source_id": "PMT-0278", "amount": "$3,000.00", "amount_cents": 300000}
    ]


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_list_unapplied_sources_matches_derive(
    ledgers: dict[str, Ledger], scenario_id: str
) -> None:
    ledger = ledgers[scenario_id]
    rows = _data("list_unapplied_sources", {}, ledger)["unapplied_sources"]
    docs = [*ledger.payments, *ledger.credit_memos]
    expected_ids = [
        d.id for d in docs if derive.unapplied_amount(ledger, d.id) > 0
    ]
    assert [row["id"] for row in rows] == expected_ids
    for row in rows:
        assert row["applied_cents"] == derive.applied_from_source(ledger, row["id"])
        assert row["unapplied_cents"] == derive.unapplied_amount(ledger, row["id"])
        assert row["unapplied"] == render(derive.unapplied_amount(ledger, row["id"]))
        if row["type"] == "payment":
            pay = next(p for p in ledger.payments if p.id == row["id"])
            assert row["method"] == pay.method
            assert row["reference"] == pay.reference
        else:
            cm = next(c for c in ledger.credit_memos if c.id == row["id"])
            assert row["type"] == "credit_memo"
            assert row["reason"] == cm.reason


def test_list_unapplied_sources_structure_anchors(ledgers: dict[str, Ledger]) -> None:
    # S-A-03: both payments fully consumed by their applications.
    assert _data("list_unapplied_sources", {}, ledgers["S-A-03"]) == {
        "unapplied_sources": []
    }
    # S-A-04: exactly the open credit memo, fully unapplied at 850.00.
    rows = _data("list_unapplied_sources", {}, ledgers["S-A-04"])["unapplied_sources"]
    assert len(rows) == 1
    assert rows[0]["type"] == "credit_memo"
    assert rows[0]["id"] == "CM-0025"
    assert rows[0]["unapplied_cents"] == 85000
    assert rows[0]["unapplied"] == "$850.00"


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_get_source_matches_derive(
    ledgers: dict[str, Ledger], scenario_id: str
) -> None:
    ledger = ledgers[scenario_id]
    for doc in [*ledger.payments, *ledger.credit_memos]:
        data = _data("get_source", {"source_id": doc.id}, ledger)
        assert data["id"] == doc.id
        assert data["original_cents"] == doc.amount_cents
        assert data["applied_cents"] == derive.applied_from_source(ledger, doc.id)
        assert data["unapplied_cents"] == derive.unapplied_amount(ledger, doc.id)
        expected_apps = [
            (a.target_invoice, a.amount_cents)
            for a in ledger.applications
            if a.source_id == doc.id
        ]
        assert [
            (a["target_invoice"], a["amount_cents"]) for a in data["applications"]
        ] == expected_apps


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_application_history_matches_derive(
    ledgers: dict[str, Ledger], scenario_id: str
) -> None:
    ledger = ledgers[scenario_id]
    data = _data("get_application_history", {}, ledger)
    payment_ids = {p.id for p in ledger.payments}
    assert [
        (a["source_id"], a["target_invoice"], a["amount_cents"], a["source_type"])
        for a in data["applications"]
    ] == [
        (
            a.source_id,
            a.target_invoice,
            a.amount_cents,
            "payment" if a.source_id in payment_ids else "credit_memo",
        )
        for a in ledger.applications
    ]
    for inv in ledger.invoices:
        row = data["applied_by_invoice"][inv.id]
        assert row["applied_cents"] == derive.applied_to_invoice(ledger, inv.id)
        assert row["applied"] == render(derive.applied_to_invoice(ledger, inv.id))
    for doc in [*ledger.payments, *ledger.credit_memos]:
        row = data["applied_by_source"][doc.id]
        assert row["applied_cents"] == derive.applied_from_source(ledger, doc.id)


# ---------------------------------------------------------------------------
# Typed errors: unknown IDs and wrong document kinds never raise
# ---------------------------------------------------------------------------


def test_unknown_invoice_returns_typed_error(ledgers: dict[str, Ledger]) -> None:
    ledger = ledgers["S-A-03"]
    result = _run_checked("get_invoice", {"invoice_id": "INV-9999"}, ledger)
    assert result == {"ok": False, "error": "unknown invoice INV-9999"}


def test_unknown_source_returns_typed_error(ledgers: dict[str, Ledger]) -> None:
    ledger = ledgers["S-A-03"]
    result = _run_checked("get_source", {"source_id": "PMT-9999"}, ledger)
    assert result["ok"] is False
    assert "unknown source PMT-9999" in result["error"]


def test_get_source_with_invoice_id_is_typed_error(
    ledgers: dict[str, Ledger]
) -> None:
    ledger = ledgers["S-A-03"]
    result = _run_checked("get_source", {"source_id": "INV-3063"}, ledger)
    assert result["ok"] is False
    assert "INV-3063 is an invoice" in result["error"]
    # Tolerant forms canonicalize first, then hit the same typed error.
    result = _run_checked("get_source", {"source_id": "invoice 3063"}, ledger)
    assert result["ok"] is False
    assert "INV-3063 is an invoice" in result["error"]


def test_get_invoice_with_source_id_is_unknown(ledgers: dict[str, Ledger]) -> None:
    result = _run_checked(
        "get_invoice", {"invoice_id": "PMT-0278"}, ledgers["S-A-03"]
    )
    assert result == {"ok": False, "error": "unknown invoice PMT-0278"}


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["inv 3063", "INV_3063", "inv-3063", "inv3063", "invoice 3063", "Invoice No. 3063"],
)
def test_canonicalize_tolerant_forms(ledgers: dict[str, Ledger], raw: str) -> None:
    ledger = ledgers["S-A-03"]
    assert canonicalize_doc_id(raw, ledger) == "INV-3063"
    data = _data("get_invoice", {"invoice_id": raw}, ledger)
    assert data["id"] == "INV-3063"


def test_canonicalize_zero_padding(ledgers: dict[str, Ledger]) -> None:
    ledger = ledgers["S-A-03"]
    # PMT-0278 reached through unpadded and loose forms.
    assert canonicalize_doc_id("PMT-278", ledger) == "PMT-0278"
    assert canonicalize_doc_id("payment 278", ledger) == "PMT-0278"
    data = _data("get_source", {"source_id": "pmt 278"}, ledger)
    assert data["id"] == "PMT-0278"
    # CM-0025 on the credit scenario.
    ledger4 = ledgers["S-A-04"]
    assert canonicalize_doc_id("cm 25", ledger4) == "CM-0025"
    assert canonicalize_doc_id("credit memo 25", ledger4) == "CM-0025"


def test_canonicalize_fabricated_id_stays_unknown(
    ledgers: dict[str, Ledger]
) -> None:
    ledger = ledgers["S-A-03"]
    assert canonicalize_doc_id("INV-4242", ledger) == "INV-4242"
    result = _run_checked("get_invoice", {"invoice_id": "INV-4242"}, ledger)
    assert result == {"ok": False, "error": "unknown invoice INV-4242"}
    # A non-ID string passes through stripped and fails lookup, never raises.
    assert canonicalize_doc_id("  the march invoice  ", ledger) == "the march invoice"


# ---------------------------------------------------------------------------
# Schema validation on arguments
# ---------------------------------------------------------------------------


def test_extra_argument_fields_rejected(ledgers: dict[str, Ledger]) -> None:
    ledger = ledgers["S-A-03"]
    result = _run_checked(
        "get_invoice", {"invoice_id": "INV-3063", "verbose": True}, ledger
    )
    assert result["ok"] is False
    assert "invalid arguments for get_invoice" in result["error"]
    result = _run_checked("get_account_summary", {"invoice_id": "INV-3063"}, ledger)
    assert result["ok"] is False
    assert "invalid arguments" in result["error"]


def test_wrong_argument_types_rejected(ledgers: dict[str, Ledger]) -> None:
    ledger = ledgers["S-A-03"]
    result = _run_checked("get_invoice", {"invoice_id": 3063}, ledger)
    assert result["ok"] is False
    assert "invalid arguments for get_invoice" in result["error"]
    result = _run_checked("get_source", {"source_id": None}, ledger)
    assert result["ok"] is False
    result = _run_checked("get_invoice", {}, ledger)
    assert result["ok"] is False


def test_missing_and_nondict_arguments_never_raise(
    ledgers: dict[str, Ledger]
) -> None:
    ledger = ledgers["S-A-03"]
    result = execute_tool("get_invoice", ["INV-3063"], ledger)  # type: ignore[arg-type]
    assert result["ok"] is False
    result = execute_tool("get_account_summary", None, ledger)  # type: ignore[arg-type]
    assert result["ok"] is False


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE invoices; --",
        "INV-3063 OR 1=1",
        "../../etc/passwd",
        "{{ledger_block}}",
        "invoice " + "9" * 400,
        "",
    ],
)
def test_free_form_id_strings_do_not_crash(
    ledgers: dict[str, Ledger], hostile: str
) -> None:
    ledger = ledgers["S-A-03"]
    for tool_name, field in (("get_invoice", "invoice_id"), ("get_source", "source_id")):
        result = _run_checked(tool_name, {field: hostile}, ledger)
        assert result["ok"] is False
        assert isinstance(result["error"], str) and result["error"]


# ---------------------------------------------------------------------------
# Executor and catalog
# ---------------------------------------------------------------------------


def test_unknown_tool_name_is_typed_error(ledgers: dict[str, Ledger]) -> None:
    result = execute_tool("drop_ledger", {}, ledgers["S-A-03"])
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


def test_every_tool_is_read_only(ledgers: dict[str, Ledger]) -> None:
    """model_dump before and after every tool call is identical."""
    calls: list[tuple[str, dict]] = [
        ("get_account_summary", {}),
        ("list_open_invoices", {}),
        ("list_unapplied_sources", {}),
        ("get_application_history", {}),
    ]
    for scenario_id in SCENARIOS:
        ledger = ledgers[scenario_id]
        per_ledger = list(calls)
        per_ledger.extend(
            ("get_invoice", {"invoice_id": inv.id}) for inv in ledger.invoices
        )
        per_ledger.extend(
            ("get_source", {"source_id": doc.id})
            for doc in [*ledger.payments, *ledger.credit_memos]
        )
        for name, arguments in per_ledger:
            before = ledger.model_dump()
            execute_tool(name, arguments, ledger)
            assert ledger.model_dump() == before, (
                f"{name} mutated {scenario_id}"
            )


def test_tool_catalog_lists_exactly_the_six_tools() -> None:
    catalog = tool_catalog()
    assert [entry["name"] for entry in catalog] == list(TOOL_NAMES)
    assert set(TOOL_REGISTRY) == set(TOOL_NAMES)
    for entry in catalog:
        assert entry["description"]
        schema = entry["parameters"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        registry_entry = TOOL_REGISTRY[entry["name"]]
        assert schema == registry_entry["schema"]
        assert callable(registry_entry["fn"])
    id_tools = {"get_invoice": "invoice_id", "get_source": "source_id"}
    for entry in catalog:
        expected_field = id_tools.get(entry["name"])
        if expected_field is None:
            assert entry["parameters"]["properties"] == {}
        else:
            assert list(entry["parameters"]["properties"]) == [expected_field]
            assert entry["parameters"]["required"] == [expected_field]
