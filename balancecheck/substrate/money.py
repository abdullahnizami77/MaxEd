"""Money as integer cents, everywhere.

Invariant I1: no money value is ever a float. `Cents` is a NewType over int;
parsing happens once at the fixture boundary, rendering happens once at the
draft surface, and nothing in this system divides money into money. If a
future feature divides (none here does), the policy is round-half-even,
stated now so the question is pre-answered rather than dodged.

Percentages, where prose needs them, are computed as ratios of two integer
cent values and rendered as whole percents; money itself is never divided.
"""

from __future__ import annotations

import re
from typing import NewType

Cents = NewType("Cents", int)

# Strict by design: at least one digit is required (",,," must not parse to
# $0.00) and commas, when present, must be standard thousands grouping (a
# typo like "$6,30.00" raises instead of silently becoming $630.00).
_AMOUNT_RE = re.compile(
    r"^-?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$"
)


def cents(n: int) -> Cents:
    """Assert-and-wrap: the runtime boundary check backing I1."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"money must be an int of cents, got {type(n).__name__}: {n!r}")
    return Cents(n)


def parse_dollars(text: str) -> Cents:
    """Parse a dollar string like '$1,250.00', '1250.5', '-$35' to Cents.

    This is the single parse point at the fixture boundary. It never goes
    through float: dollars and fractional cents are parsed as integers.
    """
    s = text.strip()
    if not _AMOUNT_RE.match(s):
        raise ValueError(f"unparseable dollar amount: {text!r}")
    negative = s.startswith("-")
    s = s.lstrip("-").lstrip("$").strip().replace(",", "")
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    value = int(whole or "0") * 100 + int(frac)
    return cents(-value if negative else value)


def render(amount: Cents) -> str:
    """Render Cents as '$1,250.00' ('-$150.00' for negatives).

    The single render point at the draft surface. Uses divmod on integers;
    no float is ever constructed.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError(f"render expects int cents, got {type(amount).__name__}")
    sign = "-" if amount < 0 else ""
    whole, frac = divmod(abs(amount), 100)
    return f"{sign}${whole:,}.{frac:02d}"


def whole_percent(part: Cents, total: Cents) -> int:
    """Integer percent of part over total, round-half-even, no float.

    For prose like 'you have paid 60 percent of this invoice'. Money is
    never divided into money; this is a dimensionless ratio of two cent
    values rendered as a whole percent.
    """
    if total <= 0 or part < 0:
        raise ValueError("whole_percent expects part >= 0 and total > 0")
    q, r = divmod(part * 100, total)
    if 2 * r < total:
        return q
    if 2 * r > total:
        return q + 1
    return q if q % 2 == 0 else q + 1  # exact half: round to even
