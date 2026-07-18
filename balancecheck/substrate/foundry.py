"""The fixture foundry: twelve deliberate ledgers, seeded and self-audited.

What this module guarantees:

- build_all() is a pure function of its seed. Two calls with the same seed
  yield byte-identical ledgers, so fixtures are reproducible and diffable.
- Each of the six deliberate structures is one labeled call per pool, and
  the structure_labels stored on a ledger are the builder's stated INTENT.
  build_all() asserts that intent equals the signature derive.py detects
  from the records, for every scenario, so the foundry cannot lie about
  what it built. build_all() also asserts derive.validate_ledger() returns
  no problems for every scenario.
- All money enters through money.parse_dollars at this boundary as integer
  cents (invariant I1); no float ever holds a money value here.

Pools: S-A-01..S-A-06 (Pool A, the learning pool, as_of 2026-03-31) and
S-B-01..S-B-06 (Pool B, held out, as_of 2026-04-30, structurally matched
archetype-for-archetype with different names, amounts, and dates).
Archetypes by index:

  01 clean baseline           (all six signature bits false)
  02 unapplied cash           (has_unapplied_cash)
  03 partial payment          (has_partial)
  04 open credit              (has_open_credit)
  05 near-duplicate + cutoff  (has_near_duplicate, has_cutoff_risk)
  06 ambiguous allocation     (has_unapplied_cash, has_near_duplicate,
                               has_ambiguous_allocation; the abstain target)

The seed shapes only non-load-bearing surface detail (document numbers and
payment references); the amounts, dates, and applications that carry each
structure are explicit parameters, so ambiguity is designed, not accidental.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from balancecheck.config import REPO_ROOT
from balancecheck.contracts.models import (
    STRUCTURE_LABELS,
    Application,
    ClientInfo,
    CreditMemo,
    Invoice,
    Ledger,
    Payment,
)
from balancecheck.substrate import derive
from balancecheck.substrate.money import Cents, cents, parse_dollars

FOUNDRY_SEED = 20260718
FIXTURES_DIR = REPO_ROOT / "fixtures"
INDEX_FILENAME = "index.json"

_REFERENCE_PREFIXES = {"ach": "ACH", "check": "CHK", "wire": "WIRE"}


# ---------------------------------------------------------------------------
# Per-pool context: client identity plus seeded document-number sequences
# ---------------------------------------------------------------------------


@dataclass
class _PoolContext:
    pool: Literal["A", "B"]
    client: ClientInfo
    rng: random.Random
    inv_no: int = field(init=False, default=0)
    pmt_no: int = field(init=False, default=0)
    cm_no: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.inv_no = self.rng.randint(1000, 3800)
        self.pmt_no = self.rng.randint(120, 640)
        self.cm_no = self.rng.randint(11, 48)

    def invoice(self, issued: date, amount: Cents, memo: str) -> Invoice:
        self.inv_no += self.rng.randint(3, 29)
        return Invoice(
            id=f"INV-{self.inv_no}",
            date=issued,
            due=issued + timedelta(days=self.client.terms_days),
            amount_cents=amount,
            memo=memo,
        )

    def payment(self, paid_on: date, amount: Cents, method: str = "ach") -> Payment:
        self.pmt_no += self.rng.randint(2, 17)
        prefix = _REFERENCE_PREFIXES[method]
        return Payment(
            id=f"PMT-{self.pmt_no:04d}",
            date=paid_on,
            amount_cents=amount,
            method=method,
            reference=f"{prefix}-{self.rng.randint(10000, 99999)}",
        )

    def credit(self, issued: date, amount: Cents, reason: str) -> CreditMemo:
        self.cm_no += self.rng.randint(1, 9)
        return CreditMemo(
            id=f"CM-{self.cm_no:04d}", date=issued, amount_cents=amount, reason=reason
        )


def _context(pool: Literal["A", "B"], seed: int) -> _PoolContext:
    if pool == "A":
        client = ClientInfo(
            id="CL-A",
            name="Harbor & Finch Design Co.",
            terms_days=30,
            as_of=date(2026, 3, 31),
        )
    else:
        client = ClientInfo(
            id="CL-B",
            name="Bluestem Orchard Supply LLC",
            terms_days=45,
            as_of=date(2026, 4, 30),
        )
    return _PoolContext(
        pool=pool,
        client=client,
        rng=random.Random(f"balancecheck-foundry:{seed}:{pool}"),
    )


# ---------------------------------------------------------------------------
# Shared assembly helpers
# ---------------------------------------------------------------------------


def _pay_in_full(
    ctx: _PoolContext, inv: Invoice, paid_on: date, method: str = "ach"
) -> tuple[Payment, Application]:
    pmt = ctx.payment(paid_on, cents(inv.amount_cents), method)
    app = Application(
        source_id=pmt.id, target_invoice=inv.id, amount_cents=inv.amount_cents
    )
    return pmt, app


def _ledger(
    ctx: _PoolContext,
    scenario_id: str,
    labels: list[str],
    invoices: list[Invoice],
    payments: list[Payment],
    credit_memos: list[CreditMemo],
    applications: list[Application],
) -> Ledger:
    return Ledger(
        scenario_id=scenario_id,
        pool=ctx.pool,
        structure_labels=list(labels),
        client=ctx.client,
        invoices=invoices,
        payments=payments,
        credit_memos=credit_memos,
        applications=applications,
    )


# ---------------------------------------------------------------------------
# The six archetypes: one labeled call each, with design asserts
# ---------------------------------------------------------------------------


def _clean_baseline(
    ctx: _PoolContext,
    scenario_id: str,
    *,
    paid: list[tuple[date, str, date, str]],
    open_invoices: list[tuple[date, str, str]],
) -> Ledger:
    """01: fully-paid and open invoices, nothing else; all six bits false."""
    invoices: list[Invoice] = []
    payments: list[Payment] = []
    applications: list[Application] = []
    for issued, amount, paid_on, memo in paid:
        inv = ctx.invoice(issued, parse_dollars(amount), memo)
        pmt, app = _pay_in_full(ctx, inv, paid_on)
        invoices.append(inv)
        payments.append(pmt)
        applications.append(app)
    for issued, amount, memo in open_invoices:
        invoices.append(ctx.invoice(issued, parse_dollars(amount), memo))
    return _ledger(ctx, scenario_id, [], invoices, payments, [], applications)


def _unapplied_cash(
    ctx: _PoolContext,
    scenario_id: str,
    *,
    matched_open: tuple[date, str, str],
    other_open: tuple[date, str, str],
    paid: tuple[date, str, date, str],
    remittance: tuple[date, str],
) -> Ledger:
    """02: one fully-unapplied payment (remittance without reference) whose
    amount equals the open amount of exactly one open invoice."""
    m_issued, m_amount, m_memo = matched_open
    target = ctx.invoice(m_issued, parse_dollars(m_amount), m_memo)
    o_issued, o_amount, o_memo = other_open
    other = ctx.invoice(o_issued, parse_dollars(o_amount), o_memo)
    s_issued, s_amount, s_on, s_memo = paid
    settled = ctx.invoice(s_issued, parse_dollars(s_amount), s_memo)
    pmt, app = _pay_in_full(ctx, settled, s_on)
    r_on, r_amount = remittance
    floating = ctx.payment(r_on, parse_dollars(r_amount), "check")
    ledger = _ledger(
        ctx,
        scenario_id,
        ["has_unapplied_cash"],
        [target, other, settled],
        [pmt, floating],
        [],
        [app],
    )
    unapplied = derive.unapplied_amount(ledger, floating.id)
    matches = [
        i.id for i in ledger.invoices if derive.open_amount(ledger, i.id) == unapplied
    ]
    assert matches == [target.id], (
        f"{scenario_id}: remittance must match exactly the one intended invoice, "
        f"got {matches}"
    )
    return ledger


def _partial_payment(
    ctx: _PoolContext,
    scenario_id: str,
    *,
    target: tuple[date, str, str],
    partial: tuple[date, str],
    open_invoice: tuple[date, str, str],
    paid: tuple[date, str, date, str],
) -> Ledger:
    """03: an application smaller than its invoice; the payment is fully
    consumed by the application, so no unapplied cash exists."""
    t_issued, t_amount, t_memo = target
    inv = ctx.invoice(t_issued, parse_dollars(t_amount), t_memo)
    p_on, p_amount = partial
    part = parse_dollars(p_amount)
    assert 0 < part < inv.amount_cents, (
        f"{scenario_id}: partial payment must be smaller than its invoice"
    )
    pmt = ctx.payment(p_on, part)
    app = Application(source_id=pmt.id, target_invoice=inv.id, amount_cents=part)
    o_issued, o_amount, o_memo = open_invoice
    other = ctx.invoice(o_issued, parse_dollars(o_amount), o_memo)
    s_issued, s_amount, s_on, s_memo = paid
    settled = ctx.invoice(s_issued, parse_dollars(s_amount), s_memo)
    s_pmt, s_app = _pay_in_full(ctx, settled, s_on)
    return _ledger(
        ctx,
        scenario_id,
        ["has_partial"],
        [inv, other, settled],
        [pmt, s_pmt],
        [],
        [app, s_app],
    )


def _open_credit(
    ctx: _PoolContext,
    scenario_id: str,
    *,
    open_invoices: list[tuple[date, str, str]],
    paid: tuple[date, str, date, str],
    credit: tuple[date, str, str],
) -> Ledger:
    """04: one credit memo with no applications; net_balance subtracts it.
    The credit amount matches no open invoice amount, avoiding ambiguity."""
    invoices = [ctx.invoice(i, parse_dollars(a), m) for i, a, m in open_invoices]
    s_issued, s_amount, s_on, s_memo = paid
    settled = ctx.invoice(s_issued, parse_dollars(s_amount), s_memo)
    pmt, app = _pay_in_full(ctx, settled, s_on)
    c_issued, c_amount, c_reason = credit
    credit_cents = parse_dollars(c_amount)
    cm = ctx.credit(c_issued, credit_cents, c_reason)
    ledger = _ledger(
        ctx,
        scenario_id,
        ["has_open_credit"],
        [*invoices, settled],
        [pmt],
        [cm],
        [app],
    )
    open_amounts = {derive.open_amount(ledger, i.id) for i in ledger.invoices}
    assert credit_cents not in open_amounts, (
        f"{scenario_id}: credit amount must not equal any open invoice amount"
    )
    return ledger


def _near_duplicate_cutoff(
    ctx: _PoolContext,
    scenario_id: str,
    *,
    dup_amount: str,
    open_dup: tuple[date, str],
    paid_dup: tuple[date, date, str],
    extra_open: tuple[date, str, str],
) -> Ledger:
    """05: two invoices at the identical amount (one paid, one open, so no
    allocation ambiguity) plus a fully-applied payment dated inside the
    cutoff window; an applied payment near as_of still carries cutoff risk,
    which is the design."""
    amount = parse_dollars(dup_amount)
    o_issued, o_memo = open_dup
    open_inv = ctx.invoice(o_issued, amount, o_memo)
    p_issued, paid_on, p_memo = paid_dup
    paid_inv = ctx.invoice(p_issued, amount, p_memo)
    pmt, app = _pay_in_full(ctx, paid_inv, paid_on, "wire")
    e_issued, e_amount, e_memo = extra_open
    extra = ctx.invoice(e_issued, parse_dollars(e_amount), e_memo)
    cutoff_edge = ctx.client.as_of - timedelta(days=derive.CUTOFF_WINDOW_DAYS)
    assert cutoff_edge <= paid_on <= ctx.client.as_of, (
        f"{scenario_id}: the applied payment must sit inside the cutoff window"
    )
    return _ledger(
        ctx,
        scenario_id,
        ["has_near_duplicate", "has_cutoff_risk"],
        [open_inv, paid_inv, extra],
        [pmt],
        [],
        [app],
    )


def _ambiguous_allocation(
    ctx: _PoolContext,
    scenario_id: str,
    *,
    dup_amount: str,
    open_dups: tuple[tuple[date, str], tuple[date, str]],
    paid: tuple[date, str, date, str],
    remittance_on: date,
) -> Ledger:
    """06: two open invoices at the same open amount plus one fully-unapplied
    payment at exactly that amount; the records cannot say which invoice the
    money was meant for (the abstain target)."""
    amount = parse_dollars(dup_amount)
    (i1_issued, i1_memo), (i2_issued, i2_memo) = open_dups
    inv1 = ctx.invoice(i1_issued, amount, i1_memo)
    inv2 = ctx.invoice(i2_issued, amount, i2_memo)
    s_issued, s_amount, s_on, s_memo = paid
    settled = ctx.invoice(s_issued, parse_dollars(s_amount), s_memo)
    pmt, app = _pay_in_full(ctx, settled, s_on)
    floating = ctx.payment(remittance_on, amount, "check")
    ledger = _ledger(
        ctx,
        scenario_id,
        ["has_unapplied_cash", "has_near_duplicate", "has_ambiguous_allocation"],
        [inv1, inv2, settled],
        [pmt, floating],
        [],
        [app],
    )
    amb = derive.ambiguous_allocations(ledger)
    assert len(amb) == 1 and set(amb[0][1]) == {inv1.id, inv2.id}, (
        f"{scenario_id}: expected exactly one two-candidate ambiguity, got {amb}"
    )
    return ledger


# ---------------------------------------------------------------------------
# The twelve scenarios
# ---------------------------------------------------------------------------


def _build_pool_a(seed: int) -> list[Ledger]:
    ctx = _context("A", seed)
    return [
        _clean_baseline(
            ctx,
            "S-A-01",
            paid=[
                (date(2026, 1, 12), "$4,850.00", date(2026, 2, 18), "Brand identity refresh, phase 1"),
                (date(2026, 1, 28), "$760.00", date(2026, 3, 6), "Print collateral reorder"),
            ],
            open_invoices=[
                (date(2026, 2, 3), "$12,400.00", "Website redesign, milestone 2"),
                (date(2026, 3, 2), "$1,975.50", "Q1 social media asset library"),
            ],
        ),
        _unapplied_cash(
            ctx,
            "S-A-02",
            matched_open=(date(2026, 1, 20), "$6,300.00", "Packaging concepts, spring line"),
            other_open=(date(2026, 2, 11), "$2,150.00", "Trade show banner set"),
            paid=(date(2026, 1, 8), "$9,875.50", date(2026, 2, 6), "Annual brand guidelines update"),
            remittance=(date(2026, 3, 10), "$6,300.00"),
        ),
        _partial_payment(
            ctx,
            "S-A-03",
            target=(date(2026, 1, 15), "$8,200.00", "Office signage program"),
            partial=(date(2026, 2, 20), "$3,000.00"),
            open_invoice=(date(2026, 2, 26), "$1,450.50", "Menu and wayfinding revisions"),
            paid=(date(2026, 1, 5), "$5,600.00", date(2026, 2, 2), "Product photography day rate"),
        ),
        _open_credit(
            ctx,
            "S-A-04",
            open_invoices=[
                (date(2026, 2, 14), "$3,750.00", "Email template system"),
                (date(2026, 1, 27), "$11,200.00", "Rebrand rollout, retail fixtures"),
            ],
            paid=(date(2026, 1, 9), "$2,400.50", date(2026, 2, 10), "Stationery print run"),
            credit=(date(2026, 3, 2), "$850.00", "Returned display easels, restock credit"),
        ),
        _near_duplicate_cutoff(
            ctx,
            "S-A-05",
            dup_amount="$5,400.00",
            open_dup=(date(2026, 2, 10), "Monthly retainer, February"),
            paid_dup=(date(2026, 2, 24), date(2026, 3, 28), "Monthly retainer, March"),
            extra_open=(date(2026, 1, 30), "$1,825.00", "Icon set licensing"),
        ),
        _ambiguous_allocation(
            ctx,
            "S-A-06",
            dup_amount="$7,250.00",
            open_dups=(
                (date(2026, 2, 5), "Catalog layout, spring edition"),
                (date(2026, 3, 3), "Catalog layout, summer edition"),
            ),
            paid=(date(2026, 1, 22), "$3,900.50", date(2026, 2, 17), "Illustration commission"),
            remittance_on=date(2026, 3, 12),
        ),
    ]


def _build_pool_b(seed: int) -> list[Ledger]:
    ctx = _context("B", seed)
    return [
        _clean_baseline(
            ctx,
            "S-B-01",
            paid=[
                (date(2026, 2, 9), "$7,340.00", date(2026, 3, 16), "Trellis wire and posts, spring order"),
                (date(2026, 2, 25), "$1,120.00", date(2026, 4, 2), "Pruning saw restock"),
            ],
            open_invoices=[
                (date(2026, 3, 4), "$15,750.50", "Drip irrigation system, block 4"),
                (date(2026, 4, 1), "$2,980.00", "Pollination hive rental"),
            ],
        ),
        _unapplied_cash(
            ctx,
            "S-B-02",
            matched_open=(date(2026, 2, 18), "$4,725.00", "Frost fan annual service"),
            other_open=(date(2026, 3, 9), "$9,340.50", "Harvest bin replacement lot"),
            paid=(date(2026, 2, 2), "$6,180.00", date(2026, 3, 6), "Sprayer parts and calibration"),
            remittance=(date(2026, 4, 10), "$4,725.00"),
        ),
        _partial_payment(
            ctx,
            "S-B-03",
            target=(date(2026, 2, 12), "$12,600.00", "Cold storage racking install"),
            partial=(date(2026, 3, 24), "$5,000.00"),
            open_invoice=(date(2026, 3, 30), "$3,275.50", "Orchard ladder replacement set"),
            paid=(date(2026, 2, 4), "$8,140.00", date(2026, 3, 2), "Soil amendment bulk delivery"),
        ),
        _open_credit(
            ctx,
            "S-B-04",
            open_invoices=[
                (date(2026, 3, 6), "$6,850.00", "Netting and hail protection"),
                (date(2026, 2, 19), "$14,300.50", "Grafting stock, spring program"),
            ],
            paid=(date(2026, 1, 29), "$3,120.00", date(2026, 2, 24), "Irrigation filter cartridges"),
            credit=(date(2026, 4, 3), "$1,275.00", "Damaged trellis anchors, quality credit"),
        ),
        _near_duplicate_cutoff(
            ctx,
            "S-B-05",
            dup_amount="$8,900.00",
            open_dup=(date(2026, 3, 11), "Fertilizer program, installment 2"),
            paid_dup=(date(2026, 3, 18), date(2026, 4, 27), "Fertilizer program, installment 3"),
            extra_open=(date(2026, 2, 27), "$2,415.50", "Bin liner and pallet wrap"),
        ),
        _ambiguous_allocation(
            ctx,
            "S-B-06",
            dup_amount="$5,675.00",
            open_dups=(
                (date(2026, 3, 5), "Rootstock order, lot 12"),
                (date(2026, 3, 26), "Rootstock order, lot 14"),
            ),
            paid=(date(2026, 2, 10), "$9,480.50", date(2026, 3, 13), "Windbreak seedling delivery"),
            remittance_on=date(2026, 4, 8),
        ),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detected_labels(ledger: Ledger) -> list[str]:
    """The labels derive.signature() detects true, in canonical order."""
    sig = derive.signature(ledger)
    return [label for label in STRUCTURE_LABELS if sig[label]]


def build_all(seed: int = FOUNDRY_SEED) -> list[Ledger]:
    """Build all twelve scenarios and audit every one before returning.

    The audit is the honesty guarantee: stored intent labels must equal the
    detected signature, and structural validation must be clean. A foundry
    change that breaks a designed structure fails here, not downstream.
    """
    ledgers = [*_build_pool_a(seed), *_build_pool_b(seed)]
    ids = [ledger.scenario_id for ledger in ledgers]
    assert len(ids) == 12 and len(set(ids)) == 12, "scenario ids must be 12 and unique"
    for ledger in ledgers:
        problems = derive.validate_ledger(ledger)
        assert not problems, f"{ledger.scenario_id}: structural problems: {problems}"
        detected = detected_labels(ledger)
        assert detected == ledger.structure_labels, (
            f"{ledger.scenario_id}: foundry claimed {ledger.structure_labels}, "
            f"records show {detected}"
        )
    return ledgers


def write_fixtures(fixtures_dir: Path) -> None:
    """Write one <scenario_id>.json per ledger plus index.json."""
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, object]] = []
    for ledger in build_all():
        path = fixtures_dir / f"{ledger.scenario_id}.json"
        path.write_text(ledger.model_dump_json(indent=2) + "\n", encoding="utf-8")
        index.append(
            {
                "scenario_id": ledger.scenario_id,
                "pool": ledger.pool,
                "structure_labels": ledger.structure_labels,
                "ref": ledger.ref(),
            }
        )
    index_path = fixtures_dir / INDEX_FILENAME
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")


def load_ledger(scenario_id: str, fixtures_dir: Path) -> Ledger:
    """Load one written fixture back through full model validation."""
    path = fixtures_dir / f"{scenario_id}.json"
    return Ledger.model_validate_json(path.read_text(encoding="utf-8"))


def load_pool(pool: str, fixtures_dir: Path) -> list[Ledger]:
    """Load every fixture of one pool, in index order."""
    if pool not in ("A", "B"):
        raise ValueError(f"unknown pool {pool!r}; expected 'A' or 'B'")
    index_path = fixtures_dir / INDEX_FILENAME
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return [
        load_ledger(row["scenario_id"], fixtures_dir)
        for row in index
        if row["pool"] == pool
    ]


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURES_DIR
    write_fixtures(out_dir)
    written = json.loads((out_dir / INDEX_FILENAME).read_text(encoding="utf-8"))
    print(f"wrote {len(written)} ledgers and {INDEX_FILENAME} to {out_dir}")
