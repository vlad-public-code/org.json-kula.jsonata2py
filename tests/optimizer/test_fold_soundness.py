"""Constant folding must not change what an expression means.

The official suite cannot catch a bad fold: its expressions are all
type-correct, so a rule that silently *drops an error* (or an undefined)
still produces the expected value and the case passes. Every test here is
therefore an asymmetric-type or missing-operand case -- the shapes where an
identity rule stops being an identity.

The rules that were removed, and why each is unsound for a non-literal `x`:

    x + 0, x - 0, 0 + x   only if x is a number  -- "abc" + 0 must be T2001
    x * 1, x / 1          only if x is a number  -- "abc" * 1 must be T2001
    x * 0  -> 0           only if x is a number, and only if x is defined:
                          nothere * 0 must stay undefined, not become 0
    x & "" -> x           only if x is a string  -- 5 & "" is the STRING "5"

When both operands are literals the _fold_num_num / _fold_str_str paths
handle them, so nothing below costs the optimizer a real folding opportunity
-- test_literal_folding_still_happens pins that.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataCompilationError, JsonataEvaluationError
from jsonata2py.runtime.values import MISSING

_FACTORY = jsonata.JsonataExpressionFactory()

_DATA = {"s": "abc", "n": 5}


@pytest.mark.parametrize(
    "expr,code",
    [
        ("s * 0", "T2001"),
        ("s + 0", "T2001"),
        ("s - 0", "T2001"),
        ("s * 1", "T2001"),
        ("s / 1", "T2001"),
        ("0 + s", "T2002"),
        ("0 * s", "T2002"),
        ("1 * s", "T2002"),
    ],
)
def test_identity_fold_must_not_swallow_a_type_error(expr: str, code: str) -> None:
    with pytest.raises(JsonataEvaluationError) as exc_info:
        _FACTORY.compile(expr).evaluate(_DATA)
    assert exc_info.value.error_code == code


@pytest.mark.parametrize("expr", ["nothere * 0", "0 * nothere", "nothere + 0", "nothere * 1", "nothere - 0"])
def test_absorption_fold_must_not_invent_a_value_for_undefined(expr: str) -> None:
    assert _FACTORY.compile(expr).evaluate(_DATA) is MISSING


@pytest.mark.parametrize("expr", ['n & ""', '"" & n'])
def test_concat_identity_fold_must_not_drop_the_string_coercion(expr: str) -> None:
    result = _FACTORY.compile(expr).evaluate(_DATA)
    assert result == "5"
    assert isinstance(result, str), "& must produce a string, not the raw number"


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("n + 0", 5),
        ("n - 0", 5),
        ("n * 1", 5),
        ("n / 1", 5),
        ("n * 0", 0),
        ("0 + n", 5),
        ("1 * n", 5),
    ],
)
def test_numeric_operands_are_unaffected(expr: str, expected: object) -> None:
    """Removing the rules must not change the answer when x really is a
    number -- these went through the fold before and through the runtime now."""
    assert _FACTORY.compile(expr).evaluate(_DATA) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("1 + 1", 2),
        ("2 * 3", 6),
        ("5 - 0 * 1", 5),
        ("10 / 2", 5),
        ('"a" & ""', "a"),
        ('"" & "b"', "b"),
        ("true and false", False),
        ("false or true", True),
    ],
)
def test_literal_folding_still_happens(expr: str, expected: object) -> None:
    assert _FACTORY.compile(expr).evaluate({}) == expected


# =============================================================================
# Folding to a non-finite value (see the isfinite guards in _fold_num_num and
# translator.visit_number_literal)
# =============================================================================


@pytest.mark.parametrize(
    "expr",
    [
        "1e308 + 1e308",
        "-1e308 - 1e308",
        "1e308 * 10",
        "$x := 1e308 + 1e308",
        "1e308 + 1e308 = 0",
        "1/(10e300 * 10e100)",
    ],
)
def test_overflowing_arithmetic_never_crashes_compile(expr: str) -> None:
    """math.floor(inf) raises OverflowError, so a NumberLiteral(inf) reaching
    codegen used to escape compile() as a raw OverflowError -- breaking the
    documented "compile() raises JsonataCompilationError" contract. Whatever
    the expression does, it must not raise anything outside the JSONata
    error hierarchy."""
    try:
        result = _FACTORY.compile(expr).evaluate({})
    except (JsonataCompilationError, JsonataEvaluationError):
        return
    assert result is not None
