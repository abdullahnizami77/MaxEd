"""Orchestration tests for the bounded agentic revision recovery.

Hermetic by construction: a ScriptedClient supplies pre-parsed agent steps
(no model, no network), a FakeExecutor serves deterministic tool results
computed from a real foundry ledger via derive, and every event lands in a
tmp_path log. The orchestrator's budgets, coverage enforcement, dedup
cache, forced-final path, and one-ToolCallEvent-per-attempt logging are all
asserted against that log.

The real balancecheck.drafting.tools module is built in parallel; when it
is absent a sys.modules shim provides the frozen API surface (TOOL_REGISTRY,
execute_tool, tool_catalog) so these tests never depend on it. Every test
injects its own executor, so the shim's executor is never actually called.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

from balancecheck.config import Config
from balancecheck.contracts.models import Ledger, ToolCallEvent
from balancecheck.spine.events import read_events
from balancecheck.spine.trace import prompt_sha as compute_prompt_sha
from balancecheck.substrate import derive
from balancecheck.substrate.foundry import build_all
from balancecheck.substrate.money import cents, render


def _ensure_tools_module() -> None:
    """Install a frozen-API shim only if the parallel tools module is absent."""
    try:
        import balancecheck.drafting.tools  # noqa: F401

        return
    except ImportError:
        pass

    def _unbuilt_executor(name: str, arguments: dict, ledger: Ledger) -> dict:
        return {"ok": False, "error": "tools module not built in this environment"}

    def _tool_catalog() -> list[dict]:
        return [
            {"name": "get_account_summary", "arguments": {}},
            {"name": "list_open_invoices", "arguments": {}},
            {"name": "list_unapplied_sources", "arguments": {}},
            {"name": "get_invoice", "arguments": {"id": "string"}},
            {"name": "get_source", "arguments": {"id": "string"}},
        ]

    shim = types.ModuleType("balancecheck.drafting.tools")
    shim.TOOL_REGISTRY = {}
    shim.execute_tool = _unbuilt_executor
    shim.tool_catalog = _tool_catalog
    sys.modules["balancecheck.drafting.tools"] = shim


_ensure_tools_module()

from balancecheck.drafting.agentic import (  # noqa: E402
    AGENT_STEP_SCHEMA,
    generate_agentic_revision,
)

LEDGERS = {ledger.scenario_id: ledger for ledger in build_all()}
CLEAN = LEDGERS["S-A-01"]      # zero unapplied cash and credit
UNAPPLIED = LEDGERS["S-A-02"]  # nonzero unapplied cash

DRAFT_TEXT = (
    "Dear client team,\n\nThe corrected reply body goes here.\n\nAccounts Receivable"
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Resp:
    """ModelResponse-alike: just the attributes the orchestrator reads."""

    def __init__(self, ok: bool, parsed: Any = None, text: str = "") -> None:
        self.ok = ok
        self.parsed = parsed
        self.text = text


class ScriptedClient:
    """Returns queued parsed step dicts; records every prompt it was sent.

    A queued None simulates a client failure (resp.ok False). An exhausted
    queue also returns a failure, so a runaway loop cannot hang a test.
    """

    def __init__(self, steps: list) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.calls: list[dict] = []

    def complete(
        self,
        task: str,
        prompt: str,
        *,
        system: str = "",
        schema: dict | None = None,
        stub_key: str = "",
        meta: dict | None = None,
        temperature: int = 0,
        max_tokens: int = 0,
    ) -> _Resp:
        self.prompts.append(prompt)
        self.systems.append(system)
        self.calls.append(
            {
                "task": task,
                "schema": schema,
                "stub_key": stub_key,
                "meta": dict(meta or {}),
                "max_tokens": max_tokens,
            }
        )
        if not self.steps:
            return _Resp(ok=False)
        item = self.steps.pop(0)
        if item is None:
            return _Resp(ok=False)
        return _Resp(ok=True, parsed=item, text=json.dumps(item))


class FakeExecutor:
    """Deterministic pure tool executor over a real Ledger, via derive."""

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict]] = []

    def __call__(self, name: str, arguments: dict, ledger: Ledger) -> dict:
        self.invocations.append((name, dict(arguments)))
        if name == "get_account_summary":
            return {
                "ok": True,
                "data": {
                    "client_name": ledger.client.name,
                    "net_balance": render(derive.net_balance(ledger)),
                    "unapplied_cash": render(derive.unapplied_cash_total(ledger)),
                    "unapplied_credit": render(derive.unapplied_credit_total(ledger)),
                },
            }
        if name == "list_open_invoices":
            return {
                "ok": True,
                "data": {
                    "open_invoices": [
                        {
                            "id": inv.id,
                            "due": inv.due.isoformat(),
                            "open": render(derive.open_amount(ledger, inv.id)),
                        }
                        for inv in ledger.invoices
                        if derive.open_amount(ledger, inv.id) > 0
                    ]
                },
            }
        if name == "list_unapplied_sources":
            sources = [
                {"id": doc.id, "unapplied": render(derive.unapplied_amount(ledger, doc.id))}
                for doc in list(ledger.payments) + list(ledger.credit_memos)
                if derive.unapplied_amount(ledger, doc.id) > 0
            ]
            return {"ok": True, "data": {"unapplied_sources": sources}}
        if name == "get_invoice":
            wanted = str(arguments.get("id", ""))
            for inv in ledger.invoices:
                if inv.id == wanted:
                    return {
                        "ok": True,
                        "data": {
                            "id": inv.id,
                            "date": inv.date.isoformat(),
                            "due": inv.due.isoformat(),
                            "amount": render(cents(inv.amount_cents)),
                            "open": render(derive.open_amount(ledger, inv.id)),
                        },
                    }
            return {"ok": False, "error": f"no invoice {wanted!r}"}
        if name == "get_source":
            wanted = str(arguments.get("id", ""))
            for doc in list(ledger.payments) + list(ledger.credit_memos):
                if doc.id == wanted:
                    return {
                        "ok": True,
                        "data": {
                            "id": doc.id,
                            "date": doc.date.isoformat(),
                            "amount": render(cents(doc.amount_cents)),
                            "unapplied": render(derive.unapplied_amount(ledger, doc.id)),
                        },
                    }
            return {"ok": False, "error": f"no source {wanted!r}"}
        return {"ok": False, "error": f"unknown tool {name!r}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path, **overrides) -> Config:
    base = {
        "mode": "stub",
        "log_path": tmp_path / "events.jsonl",
        "stub_file": tmp_path / "stubs.json",
    }
    base.update(overrides)
    return Config(**base)


def tool_step(*calls: tuple[str, dict], reason: str = "gather evidence") -> dict:
    return {
        "kind": "tool_calls",
        "reason": reason,
        "tool_calls": [{"name": name, "arguments": args} for name, args in calls],
        "draft": "",
    }


def final_step(draft: str = DRAFT_TEXT, reason: str = "drafting the reply") -> dict:
    return {"kind": "final", "reason": reason, "tool_calls": [], "draft": draft}


def _run(client, cfg: Config, ledger: Ledger, executor, failed=None, exemplars=None):
    return generate_agentic_revision(
        client=client,
        cfg=cfg,
        ledger=ledger,
        previous_draft="Dear team,\nThe previous draft that failed verification.\nAccounts Receivable",
        correction="the ledger shows $1,200.00 for the first invoice, you wrote $2,100.00",
        decision_reason="itemized amounts contradict the computed totals",
        failed_subject_ids=list(failed or []),
        exemplars=list(exemplars or []),
        gen_id="g-1",
        revision_index=1,
        run_id="t",
        stub_prefix="t",
        executor=executor,
    )


def _tool_events(cfg: Config) -> list[ToolCallEvent]:
    return [e for e in read_events(cfg.log_path) if isinstance(e, ToolCallEvent)]


# ---------------------------------------------------------------------------
# Fixture premises (make the scenario choices explicit, not lucky)
# ---------------------------------------------------------------------------


def test_fixture_premises() -> None:
    assert derive.unapplied_cash_total(CLEAN) == 0
    assert derive.unapplied_credit_total(CLEAN) == 0
    assert derive.unapplied_cash_total(UNAPPLIED) > 0


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_two_steps(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(("get_account_summary", {}), ("list_open_invoices", {})),
            final_step(),
        ]
    )
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == DRAFT_TEXT
    assert result.steps == 2
    assert result.tool_calls_executed == 2
    assert result.forced_final is False
    assert result.coverage_forced == []
    assert result.prompt_sha == compute_prompt_sha(client.prompts[1])

    events = _tool_events(cfg)
    assert len(events) == 2
    assert all(e.ok for e in events)
    assert [e.tool_name for e in events] == ["get_account_summary", "list_open_invoices"]
    assert all(e.round_index == 0 and e.gen_id == "g-1" and e.revision_index == 1 for e in events)

    # The client saw the frozen schema, the documented stub keys, and meta.
    assert all(c["task"] == "agentic_revision" for c in client.calls)
    assert all(c["schema"] == AGENT_STEP_SCHEMA for c in client.calls)
    assert [c["stub_key"] for c in client.calls] == ["t:S-A-01:agent:0", "t:S-A-01:agent:1"]
    assert [c["meta"]["step"] for c in client.calls] == [0, 1]


# ---------------------------------------------------------------------------
# Per-round budget
# ---------------------------------------------------------------------------


def test_fourth_call_in_a_round_is_rejected_and_logged(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    inv_a, inv_b = CLEAN.invoices[0].id, CLEAN.invoices[1].id
    client = ScriptedClient(
        [
            tool_step(
                ("get_account_summary", {}),
                ("list_open_invoices", {}),
                ("get_invoice", {"id": inv_a}),
                ("get_invoice", {"id": inv_b}),  # the 4th: over the per-round cap
            ),
            final_step(),
        ]
    )
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == DRAFT_TEXT
    assert result.tool_calls_executed == 3  # the 4th never reached the executor
    assert len(executor.invocations) == 3

    events = _tool_events(cfg)
    assert len(events) == 4  # every attempt logged, the rejected one included
    rejected = events[3]
    assert rejected.ok is False
    assert rejected.tool_name == "get_invoice"
    assert "per-round" in rejected.error
    assert [e.call_index for e in events] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------


def test_duplicate_call_served_from_cache_without_budget(tmp_path) -> None:
    # Total budget of exactly 2: if the duplicate consumed budget, the
    # list_open_invoices call would be rejected and the final rejected for
    # missing coverage. Acceptance proves the cache hit was budget-free.
    cfg = _cfg(tmp_path, agentic_max_total_tool_calls=2)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(
                ("get_account_summary", {}),
                ("get_account_summary", {}),  # identical repeat
                ("list_open_invoices", {}),
            ),
            final_step(),
        ]
    )
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == DRAFT_TEXT
    assert result.tool_calls_executed == 2      # executor ran twice only
    assert len(executor.invocations) == 2

    events = _tool_events(cfg)
    assert len(events) == 3                     # the cached attempt is still logged
    cached = events[1]
    assert cached.ok is True
    assert cached.result["data"]["cached"] is True
    assert events[0].result["data"].get("cached") is None


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


def test_unknown_tool_is_rejected_logged_and_continues(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(
                ("bogus_tool", {"x": 1}),
                ("get_account_summary", {}),
                ("list_open_invoices", {}),
            ),
            final_step(),
        ]
    )
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == DRAFT_TEXT
    events = _tool_events(cfg)
    assert len(events) == 3
    assert events[0].tool_name == "bogus_tool"
    assert events[0].ok is False
    assert "unknown tool" in events[0].error
    assert events[1].ok is True and events[2].ok is True


# ---------------------------------------------------------------------------
# Coverage enforcement
# ---------------------------------------------------------------------------


def test_premature_final_rejected_then_accepted_after_coverage(tmp_path) -> None:
    cfg = _cfg(tmp_path, agentic_max_rounds=3)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            final_step(),  # premature: no evidence gathered yet
            tool_step(
                ("get_account_summary", {}),
                ("list_open_invoices", {}),
                ("list_unapplied_sources", {}),
            ),
            final_step(),
        ]
    )
    result = _run(client, cfg, UNAPPLIED, executor)

    # The premature final was not kept; the next prompt lists what is missing.
    assert result.text == DRAFT_TEXT
    assert result.steps == 3
    assert result.forced_final is False
    assert result.coverage_forced == []
    second_prompt = client.prompts[1]
    assert "missing required evidence" in second_prompt
    for label in ("get_account_summary", "list_open_invoices", "list_unapplied_sources"):
        assert label in second_prompt


def test_unapplied_ledger_requires_list_unapplied_sources(tmp_path) -> None:
    # Same script as a clean-ledger happy path, but on the unapplied-cash
    # ledger: summary + invoices alone must NOT satisfy coverage.
    cfg = _cfg(tmp_path, agentic_max_rounds=2)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(("get_account_summary", {}), ("list_open_invoices", {})),
            final_step(),   # rejected: list_unapplied_sources still missing
            final_step(),   # the forced final
        ]
    )
    result = _run(client, cfg, UNAPPLIED, executor)

    assert result.text == DRAFT_TEXT
    assert result.forced_final is True
    assert result.coverage_forced == ["list_unapplied_sources"]
    forced = [e for e in _tool_events(cfg) if e.round_index == -1]
    assert [e.tool_name for e in forced] == ["list_unapplied_sources"]
    assert "missing required evidence: list_unapplied_sources" in client.prompts[2]


def test_failed_inv_subject_requires_get_invoice_before_acceptance(tmp_path) -> None:
    cfg = _cfg(tmp_path, agentic_max_rounds=3)
    executor = FakeExecutor()
    inv_id = CLEAN.invoices[0].id
    client = ScriptedClient(
        [
            tool_step(("get_account_summary", {}), ("list_open_invoices", {})),
            final_step(),                          # rejected: get_invoice missing
            tool_step(("get_invoice", {"id": inv_id})),
            final_step(),                          # forced final slot, now accepted
        ]
    )
    result = _run(client, cfg, CLEAN, executor, failed=[inv_id])

    assert result.text == DRAFT_TEXT
    assert result.steps == 4
    assert result.coverage_forced == []  # the model gathered it itself
    # The rejection note names the exact document call that was missing.
    assert f"get_invoice({inv_id})" in client.prompts[2]
    inv_events = [e for e in _tool_events(cfg) if e.tool_name == "get_invoice"]
    assert len(inv_events) == 1
    assert inv_events[0].ok is True
    assert inv_events[0].arguments == {"id": inv_id}
    assert inv_events[0].round_index == 2


# ---------------------------------------------------------------------------
# Budget exhaustion and the forced-final backstop
# ---------------------------------------------------------------------------


def test_forced_coverage_and_forced_final_after_exhausted_rounds(tmp_path) -> None:
    cfg = _cfg(tmp_path)  # default: 2 rounds + 1 forced final
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(("get_account_summary", {})),
            tool_step(("get_account_summary", {})),  # dithering: a cached repeat
            final_step(),                             # only under duress
        ]
    )
    result = _run(client, cfg, UNAPPLIED, executor)

    assert result.text == DRAFT_TEXT
    assert result.steps == 3
    assert result.forced_final is True
    assert result.coverage_forced == ["list_open_invoices", "list_unapplied_sources"]
    assert result.prompt_sha == compute_prompt_sha(client.prompts[2])

    forced_events = [e for e in _tool_events(cfg) if e.round_index == -1]
    assert [e.tool_name for e in forced_events] == [
        "list_open_invoices",
        "list_unapplied_sources",
    ]
    assert all(e.ok for e in forced_events)
    assert [e.call_index for e in forced_events] == [0, 1]
    # The forced prompt demands the final unambiguously.
    assert "MUST return kind=\"final\"" in client.prompts[2]


def test_never_final_returns_empty_text(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(("get_account_summary", {})),
            tool_step(("list_open_invoices", {})),
            tool_step(("get_account_summary", {})),  # still not final at the forced step
        ]
    )
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == ""
    assert result.steps == 3
    assert result.forced_final is True
    assert result.prompt_sha == ""


def test_client_failures_all_the_way_down(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    client = ScriptedClient([None, None, None])  # every call fails
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == ""
    assert result.steps == 3
    assert result.forced_final is True
    # Coverage was still force-executed by code before the forced final.
    assert result.coverage_forced == ["get_account_summary", "list_open_invoices"]
    assert result.tool_calls_executed == 2


def test_invalid_steps_count_as_failed_and_do_not_execute_tools(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    bad_final = {"kind": "final", "reason": "r", "tool_calls": [], "draft": ""}
    bad_tools = {
        "kind": "tool_calls",
        "reason": "r",
        "tool_calls": [{"name": "get_account_summary", "arguments": {}}],
        "draft": "sneaky draft alongside tool calls",
    }
    client = ScriptedClient([bad_final, bad_tools, final_step()])
    result = _run(client, cfg, CLEAN, executor)

    assert result.text == DRAFT_TEXT
    assert result.forced_final is True
    # No model-chosen tool ever executed; only the forced coverage ran.
    model_chosen = [e for e in _tool_events(cfg) if e.round_index >= 0]
    assert model_chosen == []
    assert result.coverage_forced == ["get_account_summary", "list_open_invoices"]


# ---------------------------------------------------------------------------
# One ToolCallEvent per attempt, exactly
# ---------------------------------------------------------------------------


def test_every_attempt_produces_exactly_one_tool_call_event(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    executor = FakeExecutor()
    client = ScriptedClient(
        [
            tool_step(
                ("get_account_summary", {}),
                ("bogus_tool", {}),
                ("get_account_summary", {}),      # cached
                ("list_open_invoices", {}),        # 4th: per-round rejected
            ),
            final_step(),                          # rejected: coverage incomplete
            final_step(),                          # forced final
        ]
    )
    result = _run(client, cfg, UNAPPLIED, executor)
    assert result.text == DRAFT_TEXT

    events = _tool_events(cfg)
    # Round 0: 4 attempts (executed, failed, cached, budget-rejected).
    # Forced coverage: list_open_invoices and list_unapplied_sources.
    assert len(events) == 6
    round0 = [e for e in events if e.round_index == 0]
    forced = [e for e in events if e.round_index == -1]
    assert len(round0) == 4 and len(forced) == 2
    assert [e.ok for e in round0] == [True, False, True, False]
    # Executor invocations: summary, bogus, plus the two forced tools.
    assert len(executor.invocations) == 4
    assert result.tool_calls_executed == 4
    assert result.coverage_forced == ["list_open_invoices", "list_unapplied_sources"]


# ---------------------------------------------------------------------------
# The agent never gets the ledger for free
# ---------------------------------------------------------------------------


def test_prompts_never_contain_the_rendered_ledger_marker(tmp_path) -> None:
    cfg = _cfg(tmp_path, agentic_max_rounds=3)
    executor = FakeExecutor()

    class _Exemplar:
        final_text = "Dear client team,\nAn approved exemplar reply.\nAccounts Receivable"

    client = ScriptedClient(
        [
            final_step(),  # premature, forces a rejection note into the prompt
            tool_step(
                ("get_account_summary", {}),
                ("list_open_invoices", {}),
                ("list_unapplied_sources", {}),
            ),
            final_step(),
        ]
    )
    result = _run(client, cfg, UNAPPLIED, executor, exemplars=[_Exemplar()])

    assert result.text == DRAFT_TEXT
    assert len(client.prompts) == 3
    for prompt in client.prompts + client.systems:
        assert "NET BALANCE DUE:" not in prompt
    # The prompt carries the pieces the agent IS allowed to see.
    assert "An approved exemplar reply." in client.prompts[0]
    assert "REQUIRED CORRECTION" in client.prompts[0]
    assert "itemized amounts contradict the computed totals" in client.prompts[0]
