"""Two-stage recall-first claim extraction: the trust boundary of the system.

What this module guarantees (invariant I11): every quantitative span the
broad detector catches is either typed into a code-checkable Claim or
emitted as a Claim whose check_result is already CheckStatus.UNVERIFIABLE.
Nothing detected is silently dropped, and nothing quantitative is ever
routed to the model. The failure mode "the draft asserted a number the
parser could not pin, so nobody checked it" is converted from a silent miss
into an escalation the gate must handle.

Stage 1 (the broad detector) is tuned to over-flag. Per sentence it finds:
currency amounts ($ forms, bare numbers with dollars/USD, number-word
amounts, fraction-of phrases), document IDs (canonical INV-/PMT-/CM- forms
and loose forms like "invoice 1012"), dates (ISO, month-name, M/D/YYYY),
percentages, status words in sentences that reference a document, and fuzzy
assertion sentences (conversations, agreements, approvals, promises).

Stage 2 (the typer) resolves each detected span to exactly one typed Claim.
Amounts in total/balance context become C-SUM; amounts in a sentence with
exactly one document ID become C-AMT with that subject; percentages and
fraction-of phrases become amount claims pre-marked UNVERIFIABLE (the
escalate-not-drop path); every distinct document ID yields exactly one
C-EXIST claim; fuzzy sentences yield one C-FUZZY claim each.

Public API, consumed by checks/:
- extract_claims(draft, ledger, claim_prefix) -> list[Claim]
- parse_amount_token(token) -> Cents. Both digit forms ("$2,400.00",
  "2,400 dollars", "USD 350") and number-word forms ("twelve hundred
  dollars") resolve through this one helper; it raises ValueError on
  percentages and fractions, which is exactly why those spans arrive with
  check_result already set to UNVERIFIABLE.
- parse_date_token(token) -> date, the same single parse point for the
  three date shapes the detector recognizes.

Claim.token is always the verbatim matched text for amounts, dates, and
status words; for document IDs it is the canonical upper-case ID (loose
references are canonicalized, with a deterministic ledger-assisted step
that maps zero-padding differences like "payment 208" onto PMT-0208 when
exactly one ledger document matches).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from balancecheck.contracts.models import (
    CheckResult,
    CheckStatus,
    Claim,
    ClaimType,
    Ledger,
)
from balancecheck.substrate.derive import document_ids
from balancecheck.substrate.money import Cents, cents, parse_dollars

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

_UNITS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_MULTIPLIERS: dict[str, int] = {"hundred": 100, "thousand": 1000}
_NUMBER_WORDS = sorted(set(_UNITS) | set(_MULTIPLIERS), key=len, reverse=True)
_NUMBER_WORD_ALT = "|".join(_NUMBER_WORDS)

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_LOOSE_PREFIX = {"invoice": "INV", "payment": "PMT", "credit memo": "CM"}

# ---------------------------------------------------------------------------
# Stage 1 patterns (the broad detector; tuned to over-flag)
# ---------------------------------------------------------------------------

# The lookbehinds keep "Invoice No. 9999" and "no. 12" intact: splitting
# after the abbreviation period would cut a document reference in half and
# hide it from the ID detector.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?<!No\.)(?<!no\.)\s+|\n+")

_CURRENCY_DOLLAR_RE = re.compile(r"\$\s?\d+(?:,\d{3})*(?:\.\d{1,2})?")
_CURRENCY_SUFFIX_RE = re.compile(
    r"\b\d+(?:,\d{3})*(?:\.\d{1,2})?\s*(?:dollars?|USD)\b", re.IGNORECASE
)
_CURRENCY_PREFIX_RE = re.compile(r"\bUSD\s*\d+(?:,\d{3})*(?:\.\d{1,2})?\b", re.IGNORECASE)
_WORD_AMOUNT_RE = re.compile(
    rf"\b(?:(?:{_NUMBER_WORD_ALT})(?:[\s-]+(?:and[\s-]+)?(?:{_NUMBER_WORD_ALT}))*)\s+dollars\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent\b)", re.IGNORECASE)
_FRACTION_RE = re.compile(
    r"\b(?:(?:a|one|two|three)\s+)?(?:half|third|quarter)s?\s+of\s+"
    r"(?:the\s+|this\s+|that\s+|your\s+)?(?:invoice|balance|total|amount|(?:INV|PMT|CM)-\d+)",
    re.IGNORECASE,
)

# Separator-tolerant: INV-1012, INV_9999, inv 9999, INV9999 all resolve; a
# fabricated reference in any of these shapes must still surface as C-EXIST.
_EXPLICIT_ID_RE = re.compile(r"\b(INV|PMT|CM)[-_ ]?(\d+)\b", re.IGNORECASE)
_LOOSE_ID_RE = re.compile(
    r"\b(invoice|payment|credit\s+memo)\s+(?:no\.?\s*|number\s*)?#?\s*(\d+)\b",
    re.IGNORECASE,
)

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH_DATE_RE = re.compile(
    rf"\b(?:{_MONTH_ALT})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.IGNORECASE
)
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")

_STATUS_RE = re.compile(
    r"\b(past\s+due|unpaid|paid|outstanding|overdue|settled|cleared)\b", re.IGNORECASE
)
# Paraphrases that assert a paid or zero-balance state; each canonicalizes to
# the status word "paid" so the deterministic status check applies.
_STATUS_PARAPHRASE_RE = re.compile(
    r"\b(?:taken\s+care\s+of|paid\s+off|paid\s+in\s+full|settled\s+in\s+full|"
    r"cleared\s+up|nothing\s+(?:further\s+)?(?:is\s+)?owed|no\s+longer\s+owed?|"
    r"fully\s+paid)\b",
    re.IGNORECASE,
)

_FUZZY_RE = re.compile(
    r"\b(?:approved?|approval|agreed?|agreement|waiv(?:e|ed|er|ing)|"
    r"promis(?:e|ed|es|ing)|confirm(?:s|ed|ing)?|discuss(?:ed|es|ing|ion)?|"
    r"arrang(?:e|ed|es|ing|ement)|spoke|spoken|told|call(?:ed)?|"
    r"account\s+manager|as\s+we\s+discussed|per\s+our\s+conversation|"
    r"taken\s+care\s+of|paid\s+off|settled\s+in\s+full|"
    r"nothing\s+(?:further\s+)?(?:is\s+)?owed|no\s+longer\s+owed?)\b",
    re.IGNORECASE,
)

# Bare "balance" is deliberately NOT a sum trigger: prose like "a credit memo
# of $850.00 is reflected in the balance above" attaches a document amount to
# a sentence that merely mentions the balance, and typing it C-SUM would fail
# a correct draft. The amount must be predicated OF the balance (balance
# due / balance of / balance is) or sit in another explicit total phrase.
_SUM_CONTEXT_RE = re.compile(
    r"\b(?:total|balance\s+(?:due|of|is)|amount\s+due|owes?|owed|owing)\b",
    re.IGNORECASE,
)

# Marker-free money shapes the red team walked past the detector: a comma
# grouped integer ("9,999") or a two-decimal number ("2,150.00", also inside
# parentheses) reads as money in an AR reply even with no dollar marker.
_BARE_GROUPED_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b")
_BARE_DECIMAL_RE = re.compile(r"\b\d+\.\d{2}\b")

_FRACTION_WORD_RE = re.compile(r"\b(?:half|halves|third|thirds|quarter|quarters)\b")
_PERCENT_WORD_RE = re.compile(r"%|\bpercent\b")
_CURRENCY_MARKER_RE = re.compile(r"\b(?:usd|dollars?)\b", re.IGNORECASE)
_DIGIT_AMOUNT_FULL_RE = re.compile(r"-?\$?\s*[\d,]+(?:\.\d{1,2})?")

# Ranks break position ties deterministically within a sentence.
_RANK_EXIST = 0
_RANK_AMOUNT = 1
_RANK_DATE = 2
_RANK_STATUS = 3
_RANK_FUZZY = 4


# ---------------------------------------------------------------------------
# Public parse helpers (checks/ calls these; one parse point per token shape)
# ---------------------------------------------------------------------------


def parse_amount_token(token: str) -> Cents:
    """Resolve any amount token the detector emits to integer cents.

    Handles "$2,400.00", "2,400 dollars", "USD 350", "350 USD", and
    number-word forms like "twelve hundred dollars". Raises ValueError for
    percentages, fraction phrases, and anything else that cannot resolve to
    cents; such spans are exactly the ones extract_claims pre-marks
    UNVERIFIABLE.
    """
    s = token.strip().strip(".,;:").strip()
    low = s.lower()
    if _PERCENT_WORD_RE.search(low):
        raise ValueError(f"percentage token cannot resolve to cents: {token!r}")
    if _FRACTION_WORD_RE.search(low):
        raise ValueError(f"fraction token cannot resolve to cents: {token!r}")
    cleaned = _CURRENCY_MARKER_RE.sub(" ", s).strip().strip(",").strip()
    if not cleaned:
        raise ValueError(f"empty amount token: {token!r}")
    if _DIGIT_AMOUNT_FULL_RE.fullmatch(cleaned):
        return parse_dollars(cleaned)
    return cents(_words_to_dollars(cleaned) * 100)


def parse_date_token(token: str) -> date:
    """Resolve any date token the detector emits to a datetime.date.

    Handles ISO (2026-03-31), month-name ("March 3, 2026"), and M/D/YYYY
    (3/31/2026) forms. Raises ValueError on anything else.
    """
    s = token.strip().strip(".,;:").strip()
    if _ISO_DATE_RE.fullmatch(s):
        return date.fromisoformat(s)
    m = re.fullmatch(
        rf"({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", s, re.IGNORECASE
    )
    if m:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    raise ValueError(f"unparseable date token: {token!r}")


def _words_to_dollars(text: str) -> int:
    """Parse a number-word phrase ("two thousand four hundred") to dollars."""
    tokens = [t for t in re.split(r"[\s-]+", text.lower()) if t]
    if not tokens:
        raise ValueError("empty number-word phrase")
    total = 0
    current = 0
    for word in tokens:
        if word == "and":
            continue
        if word in _UNITS:
            current += _UNITS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word == "thousand":
            total += (current or 1) * 1000
            current = 0
        else:
            raise ValueError(f"unknown number word: {word!r}")
    value = total + current
    if value <= 0:
        raise ValueError(f"number-word phrase resolved to nothing: {text!r}")
    return value


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    sent_idx: int
    pos: int
    rank: int
    type: ClaimType
    span: str
    token: str
    subject_id: str = ""
    check_result: CheckResult | None = None
    canonical_id: str = ""  # C-EXIST only, for the one-claim-per-ID dedup


@dataclass
class _IntervalSet:
    """Character intervals already claimed by a higher-priority detector."""

    taken: list[tuple[int, int]] = field(default_factory=list)

    def claim(self, start: int, end: int) -> bool:
        for s, e in self.taken:
            if start < e and end > s:
                return False
        self.taken.append((start, end))
        return True


def split_sentences(draft: str) -> list[str]:
    """Split on sentence enders and newlines; decimals never split because
    a mid-amount period is never followed by whitespace."""
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(draft) if p and p.strip()]


def extract_claims(draft: str, ledger: Ledger, claim_prefix: str = "c") -> list[Claim]:
    """Extract every checkable or escalatable claim from a draft.

    Returns claims in reading order with IDs f"{claim_prefix}-{n:03d}".
    Identical (type, span, token, subject) detections are deduplicated;
    every distinct document ID yields exactly one C-EXIST claim.
    """
    candidates: list[_Candidate] = []
    for sent_idx, sentence in enumerate(split_sentences(draft)):
        candidates.extend(_detect_in_sentence(sent_idx, sentence, ledger))

    candidates.sort(key=lambda c: (c.sent_idx, c.pos, c.rank))

    claims: list[Claim] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    seen_exist_ids: set[str] = set()
    n = 0
    for cand in candidates:
        if cand.type is ClaimType.C_EXIST:
            if cand.canonical_id in seen_exist_ids:
                continue
            seen_exist_ids.add(cand.canonical_id)
        key = (cand.type.value, cand.span, cand.token, cand.subject_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        n += 1
        claims.append(
            Claim(
                claim_id=f"{claim_prefix}-{n:03d}",
                type=cand.type,
                span=cand.span,
                token=cand.token,
                subject_id=cand.subject_id,
                check_result=cand.check_result,
            )
        )
    return claims


# ---------------------------------------------------------------------------
# Per-sentence detection and typing
# ---------------------------------------------------------------------------


def _detect_in_sentence(sent_idx: int, sentence: str, ledger: Ledger) -> list[_Candidate]:
    out: list[_Candidate] = []

    # Document IDs first: the sentence's IDs drive subject resolution.
    id_mentions: list[tuple[int, str]] = []
    for m in _EXPLICIT_ID_RE.finditer(sentence):
        id_mentions.append(
            (m.start(), _canonical_prefix_id(m.group(1).upper(), m.group(2), ledger))
        )
    for m in _LOOSE_ID_RE.finditer(sentence):
        id_mentions.append((m.start(), _canonical_loose_id(m.group(1), m.group(2), ledger)))
    id_mentions.sort()
    sentence_ids: list[str] = []
    for pos, cid in id_mentions:
        if cid not in sentence_ids:
            sentence_ids.append(cid)
            out.append(
                _Candidate(
                    sent_idx=sent_idx,
                    pos=pos,
                    rank=_RANK_EXIST,
                    type=ClaimType.C_EXIST,
                    span=sentence,
                    token=cid,
                    canonical_id=cid,
                )
            )

    subject = sentence_ids[0] if len(sentence_ids) == 1 else ""
    sum_context = bool(_SUM_CONTEXT_RE.search(sentence))

    def amount_type_and_subject() -> tuple[ClaimType, str]:
        if sum_context:
            return ClaimType.C_SUM, ""
        return ClaimType.C_AMT, subject

    # Amounts: resolvable shapes first, then percent/fraction escalations.
    # The interval set prevents one text span from becoming two amount
    # claims (e.g. "$1,200 dollars" matching both the $ and suffix forms).
    intervals = _IntervalSet()
    resolvable = (
        _CURRENCY_DOLLAR_RE,
        _CURRENCY_PREFIX_RE,
        _CURRENCY_SUFFIX_RE,
        _WORD_AMOUNT_RE,
    )
    for pattern in resolvable:
        for m in pattern.finditer(sentence):
            if not intervals.claim(m.start(), m.end()):
                continue
            token = m.group(0)
            ctype, subj = amount_type_and_subject()
            check_result: CheckResult | None = None
            try:
                parse_amount_token(token)
            except ValueError as exc:
                # Escalate-not-drop: a detected amount the typer cannot
                # resolve is still emitted, pre-marked UNVERIFIABLE (I11).
                check_result = CheckResult(
                    status=CheckStatus.UNVERIFIABLE,
                    actual=token,
                    detail=f"detected amount span could not be resolved to cents: {exc}",
                )
            out.append(
                _Candidate(
                    sent_idx=sent_idx,
                    pos=m.start(),
                    rank=_RANK_AMOUNT,
                    type=ctype,
                    span=sentence,
                    token=token,
                    subject_id=subj,
                    check_result=check_result,
                )
            )
    for pattern, kind in ((_PERCENT_RE, "percentage"), (_FRACTION_RE, "fraction-of phrase")):
        for m in pattern.finditer(sentence):
            if not intervals.claim(m.start(), m.end()):
                continue
            token = m.group(0)
            ctype, subj = amount_type_and_subject()
            # A fraction phrase names its own object ("a third of the
            # balance"): type from the object, not the surrounding sentence.
            if kind == "fraction-of phrase" and re.search(
                r"of\s+(?:the\s+|this\s+|that\s+|your\s+)?(?:balance|total|amount)\b",
                token,
                re.IGNORECASE,
            ):
                ctype, subj = ClaimType.C_SUM, ""
            out.append(
                _Candidate(
                    sent_idx=sent_idx,
                    pos=m.start(),
                    rank=_RANK_AMOUNT,
                    type=ctype,
                    span=sentence,
                    token=token,
                    subject_id=subj,
                    check_result=CheckResult(
                        status=CheckStatus.UNVERIFIABLE,
                        actual=token,
                        detail=(
                            f"detected {kind} {token!r} cannot be resolved to cents"
                            " against the ledger; escalated, not dropped (I11)"
                        ),
                    ),
                )
            )

    # Marker-free money shapes, after percent/fraction so those intervals
    # win. Dates never collide (no commas, never exactly two decimals).
    for pattern in (_BARE_GROUPED_RE, _BARE_DECIMAL_RE):
        for m in pattern.finditer(sentence):
            if not intervals.claim(m.start(), m.end()):
                continue
            token = m.group(0)
            ctype, subj = amount_type_and_subject()
            bare_check: CheckResult | None = None
            try:
                parse_amount_token(token)
            except ValueError as exc:
                bare_check = CheckResult(
                    status=CheckStatus.UNVERIFIABLE,
                    actual=token,
                    detail=f"detected amount span could not be resolved to cents: {exc}",
                )
            out.append(
                _Candidate(
                    sent_idx=sent_idx,
                    pos=m.start(),
                    rank=_RANK_AMOUNT,
                    type=ctype,
                    span=sentence,
                    token=token,
                    subject_id=subj,
                    check_result=bare_check,
                )
            )

    # Dates.
    for pattern in (_ISO_DATE_RE, _MONTH_DATE_RE, _SLASH_DATE_RE):
        for m in pattern.finditer(sentence):
            if not intervals.claim(m.start(), m.end()):
                continue
            out.append(
                _Candidate(
                    sent_idx=sent_idx,
                    pos=m.start(),
                    rank=_RANK_DATE,
                    type=ClaimType.C_DATE,
                    span=sentence,
                    token=m.group(0),
                    subject_id=subject,
                )
            )

    # Status words and paid-paraphrases, only in sentences that also
    # reference a document. A sentence naming several documents asserts the
    # status of each, so one claim is emitted per referenced document.
    if sentence_ids:
        status_hits = [(m.start(), m.group(0)) for m in _STATUS_RE.finditer(sentence)]
        status_hits += [
            (m.start(), "paid") for m in _STATUS_PARAPHRASE_RE.finditer(sentence)
        ]
        for pos, word in status_hits:
            for sid in sentence_ids:
                out.append(
                    _Candidate(
                        sent_idx=sent_idx,
                        pos=pos,
                        rank=_RANK_STATUS,
                        type=ClaimType.C_STATUS,
                        span=sentence,
                        token=word,
                        subject_id=sid,
                    )
                )

    # Fuzzy assertion sentences: one C-FUZZY per sentence.
    fuzzy = _FUZZY_RE.search(sentence)
    if fuzzy:
        out.append(
            _Candidate(
                sent_idx=sent_idx,
                pos=fuzzy.start(),
                rank=_RANK_FUZZY,
                type=ClaimType.C_FUZZY,
                span=sentence,
                token="",
            )
        )

    return out


def _canonical_prefix_id(prefix: str, digits: str, ledger: Ledger) -> str:
    """Canonicalize a prefix-form reference (INV_9999, inv 9999) to INV-9999.

    Same ledger-assisted zero-padding as loose references; an unknown number
    keeps its naive form so C-EXIST fails downstream instead of the mention
    vanishing.
    """
    naive = f"{prefix}-{digits}"
    ledger_ids = document_ids(ledger)
    if naive in ledger_ids:
        return naive
    matches = [
        d
        for d in sorted(ledger_ids)
        if d.startswith(prefix + "-") and d[len(prefix) + 1 :].lstrip("0") == digits.lstrip("0")
    ]
    if len(matches) == 1:
        return matches[0]
    return naive


def _canonical_loose_id(kind: str, digits: str, ledger: Ledger) -> str:
    """Canonicalize a loose reference like "invoice 1012" to INV-1012.

    Deterministic ledger-assisted step: when the naive canonical form is not
    a ledger document but exactly one ledger document of the same prefix
    matches once leading zeros are ignored ("payment 208" vs PMT-0208), that
    document's ID is used. Otherwise the naive form stands, so a fabricated
    reference still surfaces as a C-EXIST failure downstream.
    """
    prefix = _LOOSE_PREFIX[re.sub(r"\s+", " ", kind.lower())]
    naive = f"{prefix}-{digits}"
    ledger_ids = document_ids(ledger)
    if naive in ledger_ids:
        return naive
    matches = [
        d
        for d in sorted(ledger_ids)
        if d.startswith(prefix + "-") and d[len(prefix) + 1 :].lstrip("0") == digits.lstrip("0")
    ]
    if len(matches) == 1:
        return matches[0]
    return naive
