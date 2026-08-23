"""Generated internals must live in a namespace user variables cannot reach.

Per naming.py, a JSONata variable $name becomes the Python local v_name, and
every compiler-internal identifier takes a single leading underscore. The
holder box for a self-referential or forward-referenced binding used to break
that rule: it was emitted as v_{name}_ref, which is exactly pyvar("{name}_ref")
-- so a user expression that also bound $name_ref clobbered the holder, and
the recursive call then subscripted whatever the user had stored:

    ($f := function($x){ $x < 3 ? $f($x+1) : $x }; $f_ref := 99; $f(0))
    -> JsonataEvaluationError: 'int' object is not subscriptable

The box now lives at _ref_{name}. pyvar() can only ever emit v_..., so no
JSONata variable name can collide with it.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.translator.naming import pyvar, pyvar_ref

_FACTORY = jsonata.JsonataExpressionFactory()

_RECURSIVE = "$f := function($x){ $x < 3 ? $f($x+1) : $x }"


def test_holder_name_is_unreachable_from_pyvar() -> None:
    """The property that makes the collision impossible, asserted directly:
    no JSONata variable name maps onto a holder name."""
    for name in ["f", "f_ref", "ref", "_ref_f", "x"]:
        assert pyvar(name) != pyvar_ref("f")
    assert pyvar_ref("f").startswith("_")


@pytest.mark.parametrize(
    "expr,expected",
    [
        pytest.param(f"({_RECURSIVE}; $f_ref := 99; $f(0) & '/' & $f_ref)", "3/99", id="user-binds-f_ref"),
        pytest.param(f"({_RECURSIVE}; $f(0))", 3, id="plain-recursion"),
        pytest.param(f"({_RECURSIVE}; $f_ref := 99; $f_ref)", 99, id="f_ref-keeps-its-value"),
        pytest.param(
            "($fact := function($n){ $n <= 1 ? 1 : $n * $fact($n-1) }; $fact_ref := 7; $fact(5) + $fact_ref)",
            127,
            id="factorial-plus-colliding-name",
        ),
    ],
)
def test_user_variable_named_like_the_holder_does_not_clobber_it(expr: str, expected: object) -> None:
    assert _FACTORY.compile(expr).evaluate({}) == expected


def test_holder_name_appears_in_generated_source_only_as_an_internal() -> None:
    src = _FACTORY.translate(f"({_RECURSIVE}; $f_ref := 99; $f(0))")
    # The holder box is declared under its internal name...
    assert "_ref_f = [MISSING]" in src, "the holder box should still be emitted"
    # ...and v_f_ref belongs to the user's $f_ref alone, so the two never
    # write to the same Python local.
    assert "v_f_ref = 99" in src, "the user's $f_ref should keep the v_ namespace"
    assert "v_f_ref = [MISSING]" not in src, "the holder must not occupy the user variable namespace"
