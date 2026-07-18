"""Money substrate behavior: parse and render round-trip exactly, negatives
and comma grouping work, garbage is rejected, and whole_percent rounds
half-even. No float appears anywhere in these tests; money is integer cents.
"""

from __future__ import annotations

import pytest

from balancecheck.substrate.money import cents, parse_dollars, render, whole_percent

PARSE_CASES = [
    ("$1,250.00", 125000),
    ("$1,250", 125000),
    ("1250", 125000),
    ("1250.5", 125050),
    ("$8.5", 850),
    ("$0.50", 50),
    ("$12,400.50", 1240050),
    ("$1,234,567.89", 123456789),
    ("-$35", -3500),
    ("-$1,200.75", -120075),
    ("-150.00", -15000),
    ("$99,999.00", 9999900),
    ("  $760.00  ", 76000),
]


@pytest.mark.parametrize(("text", "expected"), PARSE_CASES)
def test_parse_dollars(text: str, expected: int) -> None:
    assert parse_dollars(text) == expected


@pytest.mark.parametrize(
    "value", [0, 5, 50, 76000, 125000, 1240050, 9999900, 123456789, -3500, -15000]
)
def test_render_then_parse_round_trips(value: int) -> None:
    assert parse_dollars(render(cents(value))) == value


def test_render_formatting() -> None:
    assert render(cents(125000)) == "$1,250.00"
    assert render(cents(1240050)) == "$12,400.50"
    assert render(cents(5)) == "$0.05"
    assert render(cents(0)) == "$0.00"
    assert render(cents(-15000)) == "-$150.00"
    assert render(cents(123456789)) == "$1,234,567.89"


@pytest.mark.parametrize(
    "garbage",
    ["", "abc", "$", "12 dollars", "$1.234", "1,2.3.4", "--5", "$-5", "12.", "(1,200)", "1.2.3"],
)
def test_parse_dollars_rejects_garbage(garbage: str) -> None:
    with pytest.raises(ValueError):
        parse_dollars(garbage)


def test_cents_rejects_non_int_money() -> None:
    with pytest.raises(TypeError):
        cents(True)
    with pytest.raises(TypeError):
        cents(float("2.0"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cents("100")  # type: ignore[arg-type]
    assert cents(1250) == 1250


def test_render_rejects_non_int_money() -> None:
    with pytest.raises(TypeError):
        render(True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        render(float("12.5"))  # type: ignore[arg-type]


def test_whole_percent_half_even_cases() -> None:
    # 1.5 percent rounds up to even 2; 2.5 percent rounds down to even 2;
    # 0.5 percent rounds down to even 0; 3.5 percent rounds up to even 4.
    assert whole_percent(cents(150), cents(10000)) == 2
    assert whole_percent(cents(250), cents(10000)) == 2
    assert whole_percent(cents(50), cents(10000)) == 0
    assert whole_percent(cents(350), cents(10000)) == 4


def test_whole_percent_exact_and_nearby() -> None:
    assert whole_percent(cents(0), cents(500)) == 0
    assert whole_percent(cents(2500), cents(10000)) == 25
    assert whole_percent(cents(6000), cents(10000)) == 60
    assert whole_percent(cents(10000), cents(10000)) == 100
    assert whole_percent(cents(333), cents(1000)) == 33
    assert whole_percent(cents(337), cents(1000)) == 34


def test_whole_percent_rejects_bad_domain() -> None:
    with pytest.raises(ValueError):
        whole_percent(cents(1), cents(0))
    with pytest.raises(ValueError):
        whole_percent(cents(-1), cents(100))
    with pytest.raises(ValueError):
        whole_percent(cents(1), cents(-100))
