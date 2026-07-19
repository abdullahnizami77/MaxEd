"""The data contracts: every record type in the system, as Pydantic models.

One source of truth. Types, validation, and JSON Schema all come from these
models; the event log is additionally validated against the exported schema
(invariant I8) because the log is the source of every reported number.

All money fields are integer cents with a _cents suffix (invariant I1).
All records carry schema_version so the log is versioned line by line.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SCHEMA_VERSION = 1

# The six deliberate ledger structures. One closed vocabulary used by the
# foundry (design intent), the signature (detected from data), the error
# foundry, and the results rows, so "the unapplied-cash case" is one
# identifier everywhere.
STRUCTURE_LABELS = (
    "has_unapplied_cash",
    "has_partial",
    "has_open_credit",
    "has_near_duplicate",
    "has_cutoff_risk",
    "has_ambiguous_allocation",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# ---------------------------------------------------------------------------
# Enums (closed vocabularies)
# ---------------------------------------------------------------------------


class ClaimType(str, Enum):
    C_EXIST = "C-EXIST"    # a document ID is referenced
    C_AMT = "C-AMT"        # an amount is attached to a document
    C_SUM = "C-SUM"        # a stated total or subtotal
    C_STATUS = "C-STATUS"  # paid / outstanding / overdue
    C_DATE = "C-DATE"      # a date claim
    C_FUZZY = "C-FUZZY"    # non-quantitative supportability claim (model-checked, isolated)


CODE_CHECKED_TYPES = frozenset(
    {ClaimType.C_EXIST, ClaimType.C_AMT, ClaimType.C_SUM, ClaimType.C_STATUS, ClaimType.C_DATE}
)


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNSUPPORTED = "unsupported"            # C-FUZZY: records contradict or do not support
    CANNOT_DETERMINE = "cannot_determine"  # C-FUZZY: records are silent; may be human-resolvable
    UNVERIFIABLE = "unverifiable"          # detector caught a quantitative span the typer could not
                                           # resolve; escalated, never dropped (invariant I11)


class GateAction(str, Enum):
    REVISE = "revise"
    ABSTAIN = "abstain"
    ESCALATE = "escalate"
    HUMAN_GATE = "human_gate"  # all checks passed; goes to the human for approval


class HumanAction(str, Enum):
    APPROVE = "approve"
    EDIT = "edit"
    DECLINE = "decline"


class GapCategory(str, Enum):
    ALLOCATION_REFERENCE = "allocation_reference"
    REMITTANCE_ADVICE = "remittance_advice"
    DOCUMENT_ABSENT = "document_absent"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    UNVERIFIABLE_QUANT = "unverifiable_quantitative_claim"
    TOOL_ABSENT = "tool_absent"
    OTHER = "other"


class InvoiceStatus(str, Enum):
    PAID = "paid"
    OUTSTANDING = "outstanding"
    OVERDUE = "overdue"


# ---------------------------------------------------------------------------
# Ledger records (applications are first-class: the schema decision the whole
# error taxonomy stands on)
# ---------------------------------------------------------------------------


class ClientInfo(StrictModel):
    id: str
    name: str
    terms_days: int
    as_of: date


class Invoice(StrictModel):
    id: str
    date: date
    due: date
    amount_cents: int = Field(gt=0)
    memo: str = ""


class Payment(StrictModel):
    id: str
    date: date
    amount_cents: int = Field(gt=0)
    method: str = "ach"
    reference: str = ""


class CreditMemo(StrictModel):
    id: str
    date: date
    amount_cents: int = Field(gt=0)
    reason: str = ""


class Application(StrictModel):
    """Applies a payment or credit memo (source) to an invoice (target).

    An unapplied payment is a payment with no (or partial) applications; a
    partial payment is an application smaller than its invoice; a
    misapplication is an application pointing at the wrong invoice. Flatten
    this away and the three most consequential real-world failures become
    unrepresentable.
    """

    source_id: str
    target_invoice: str
    amount_cents: int = Field(gt=0)


class Ledger(StrictModel):
    schema_version: int = SCHEMA_VERSION
    scenario_id: str
    pool: Literal["A", "B"]
    structure_labels: list[str] = Field(default_factory=list)  # foundry design intent
    client: ClientInfo
    invoices: list[Invoice]
    payments: list[Payment]
    credit_memos: list[CreditMemo]
    applications: list[Application]

    @field_validator("structure_labels")
    @classmethod
    def _known_labels(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(STRUCTURE_LABELS)
        if unknown:
            raise ValueError(f"unknown structure labels: {sorted(unknown)}")
        return v

    def ref(self) -> str:
        """Content hash of the canonical ledger JSON, for event attribution."""
        blob = self.model_dump_json(exclude_none=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


class CheckResult(StrictModel):
    status: CheckStatus
    expected: str = ""            # what the ledger supports (rendered, human-readable)
    actual: str = ""              # what the draft said
    cited_records: list[str] = Field(default_factory=list)  # record IDs supporting the verdict
    detail: str = ""


# Amount roles: the extractor binds each amount to the ledger figure its
# phrasing names, so the checker verifies the right quantity instead of
# guessing from substrings. "" (UNKNOWN) means a bare amount on a document
# with no role word; the checker then accepts either the document amount or
# its open/unapplied figure.
class AmountRole:
    OPEN = "open"          # open / remaining / still due / balance on an invoice
    ORIGINAL = "original"  # originally / issued for / the invoice's face amount
    APPLIED = "applied"    # paid / applied portion
    TOTAL = "total"        # the account balance or a stated subtotal (C-SUM)
    UNKNOWN = ""


class Claim(StrictModel):
    claim_id: str
    type: ClaimType
    span: str                     # the verbatim sentence from the draft
    token: str                    # the specific amount/ID/date/status text
    source: Literal["surface"] = "surface"
    check_result: CheckResult | None = None
    # The document(s) this claim is bound to, resolved by the extractor from
    # the phrasing (nearest-document binding, or the named invoices for a
    # subtotal). Checkers consume this, never re-parse the span.
    subject_ids: list[str] = Field(default_factory=list)
    # For amounts: which ledger figure the phrasing names (AmountRole).
    role: str = ""
    # Back-compat single-subject alias: the sole bound document, when there is
    # exactly one. Kept so older call sites and tests keep working.
    subject_id: str = ""


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class Finding(StrictModel):
    kind: str                     # e.g. "c_exist_fail", "unverifiable_quant", "c_amt_fail",
                                  # "fuzzy_unsupported", "fuzzy_cannot_determine", "ambiguous_allocation"
    claim_id: str = ""
    span: str = ""
    detail: str = ""
    correction: str = ""          # for revisable findings: "the ledger shows X, you wrote Y"


class GateDecision(StrictModel):
    action: GateAction
    reason: str
    findings: list[Finding] = Field(default_factory=list)
    payload: str = ""             # the exact span/claim for escalate; candidate readings for abstain


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class MemoryEntry(StrictModel):
    schema_version: int = SCHEMA_VERSION
    entry_id: str
    source_gen_id: str
    scenario_id: str
    pool: Literal["A"]            # Pool B never enters memory (invariant I7)
    signature: dict[str, bool]    # detected structure bits
    weight: float                 # edit 2.0 > approve 1.0
    final_text: str               # approved or human-corrected draft
    human_action: HumanAction
    note: str = ""                # what the edit corrected, when the human said so
    ingested_offset: int          # log offset of the consumed human_decision event


# ---------------------------------------------------------------------------
# Events: one JSONL, append-only, discriminated on event_type (invariant I8)
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventBase(StrictModel):
    schema_version: int = SCHEMA_VERSION
    ts: datetime = Field(default_factory=utcnow)
    run_id: str = ""


class RunStartEvent(EventBase):
    event_type: Literal["run_start"] = "run_start"
    pass_label: str               # "pass1" | "pass2" | "pass2R" | "errors" | "bench" | "demo" | "poolA"
    seed: int
    git_sha: str
    model_name: str
    model_digest: str
    config_hash: str
    pool: str
    memory_state_hash: str        # "empty" for pass 1; the two-line manifest diff lives here


class GenerationEvent(EventBase):
    event_type: Literal["generation"] = "generation"
    gen_id: str
    scenario_id: str
    pool: str
    pass_label: str
    ledger_ref: str
    prompt_sha: str
    draft: str
    revision_index: int           # 0 = first pass
    is_first_pass: bool
    # How this draft was produced: the cheap single-call path, a prompt-only
    # revision, or a bounded tool-using agentic recovery. Defaults keep
    # pre-existing log lines parseable.
    generation_mode: Literal[
        "prompt_initial", "prompt_revision", "agentic_recovery"
    ] = "prompt_initial"


class VerificationEvent(EventBase):
    event_type: Literal["verification"] = "verification"
    gen_id: str
    claims: list[Claim]
    decision: GateDecision


class HumanDecisionEvent(EventBase):
    event_type: Literal["human_decision"] = "human_decision"
    gen_id: str
    scenario_id: str
    pool: str
    action: HumanAction
    final_text: str               # the approved or corrected text ("" for decline)
    reason: str
    decided_at: datetime = Field(default_factory=utcnow)


class CapabilityGapEvent(EventBase):
    event_type: Literal["capability_gap"] = "capability_gap"
    gen_id: str
    scenario_id: str
    missing: GapCategory
    detail: str
    would_need: str               # what would have let the agent succeed
    resolvable_by: Literal["human", "tool", "records", "nobody"]


class IngestEvent(EventBase):
    event_type: Literal["ingest"] = "ingest"
    consumed_through_offset: int
    entries_added: int
    entries_evicted: int
    memory_state_hash: str


class ScoreEvent(EventBase):
    """Code-computed scores; the primary before/after instrument."""

    event_type: Literal["score"] = "score"
    gen_id: str
    scenario_id: str
    pass_label: str
    stage: Literal["first_pass", "final"]
    grounding_checked: int        # code-checkable claims that passed
    grounding_total: int          # code-checkable claims found
    completeness_present: int     # applicable checklist items mentioned
    completeness_total: int       # applicable checklist items
    revision_count: int
    terminal_action: str          # the gate's terminal action for this generation


class JudgmentEvent(EventBase):
    event_type: Literal["judgment"] = "judgment"
    gen_id: str = ""              # pointwise
    pair_id: str = ""             # pairwise
    mode: Literal["rubric", "pairwise"]
    ordering: str = ""            # "ab" | "ba" for pairwise
    scores: dict[str, Any] = Field(default_factory=dict)
    verdict: str = ""             # pairwise: "a" | "b" | "tie"
    prompt_sha: str
    parse_ok: bool
    judge_model: str = ""


class ToolCallEvent(EventBase):
    """One line per attempted agent tool call, including validation failures.

    The agentic recovery path may inspect the ledger through read-only tools;
    every attempt is a typed record so the log shows exactly which evidence
    the agent requested and what it received.
    """

    event_type: Literal["tool_call"] = "tool_call"
    gen_id: str
    revision_index: int
    round_index: int              # -1 marks coverage tools executed by code,
                                  # not chosen by the model (the forced-final
                                  # evidence backstop)
    call_index: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    error: str = ""


class TraceEvent(EventBase):
    """One line per model-call attempt, drafts and judge alike (invariant I6).

    The trace hook and the learning log are the same file, as the brief
    invites. Full prompt and output are kept: every inference is training
    signal.
    """

    event_type: Literal["trace"] = "trace"
    task: str
    prompt_sha: str
    prompt: str
    output: str
    model_name: str
    attempt: int
    ok: bool
    error: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


Event = Annotated[
    Union[
        RunStartEvent,
        GenerationEvent,
        VerificationEvent,
        HumanDecisionEvent,
        CapabilityGapEvent,
        IngestEvent,
        ScoreEvent,
        JudgmentEvent,
        ToolCallEvent,
        TraceEvent,
    ],
    Field(discriminator="event_type"),
]

EVENT_ADAPTER: TypeAdapter = TypeAdapter(Event)


def event_json_schema() -> dict:
    """The exported JSON Schema for a log line (used by the I8 test)."""
    return EVENT_ADAPTER.json_schema()


def parse_event(line: str):
    return EVENT_ADAPTER.validate_json(line)


def dump_event(event) -> str:
    return json.dumps(
        json.loads(event.model_dump_json(exclude_none=True)),
        separators=(",", ":"),
        sort_keys=True,
    )
