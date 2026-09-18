"""Regression tests for the defects found in the 2026-09-18 code review.

Every expectation was produced by running the same expression through the
reference `jsonata` implementation in `C:\\vlad-projects\\js\\jsonata` and
copying its result, so a future change that reverts one of these fixes fails
against ground truth rather than against a hand-written guess.

Each class names the review ID it pins.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataEvaluationError
from jsonata2py.runtime.values import MISSING


def ev(expr: str, data: object = None) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


def code(expr: str, data: object = None) -> str | None:
    with pytest.raises(JsonataEvaluationError) as e:
        ev(expr, data)
    return e.value.error_code


class TestPy1TailCallSentinel:
    """PY-1: a lambda body is compiled with is_tail_position=True and the
    flag used to propagate into every sub-expression, so `fn_apply_tco`'s
    TCO_SENTINEL was emitted where the *value* was needed."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # object-constructor value
            ('( $double := function($x){$x*2}; '
             '$wrap := function($n){ {"v": $double($n)} }; $wrap(3) )', {"v": 6}),
            # array-constructor element
            ('( $double := function($x){$x*2}; '
             '$wrap := function($n){ [$double($n)] }; $wrap(3) )', [6]),
            # path head
            ('( $mk := function($x){ {"y":$x} }; '
             '$wrap := function($n){ $mk($n).y }; $wrap(3) )', 3),
            # elvis left operand -- 0 is falsy, so the fallback must win
            ('( $z := function($x){0}; '
             '$wrap := function($n){ $z($n) ?: "fallback" }; $wrap(3) )', "fallback"),
            # binary operand
            ('( $d := function($x){$x*2}; '
             '$wrap := function($n){ $d($n) + 1 }; $wrap(3) )', 7),
            # sort key
            ('( $k := function($x){ -$x }; '
             '$wrap := function($n){ [3,1,2]^($k($)) }; $wrap(0) )', [3, 2, 1]),
            # range operand
            ('( $one := function($x){1}; '
             '$wrap := function($n){ [$one($n)..3] }; $wrap(0) )', [1, 2, 3]),
            # genuine tail position still returns the value, not the sentinel
            ('( $id := function($x){$x}; '
             '$wrap := function($n){ $id($n) }; $wrap(3) )', 3),
        ],
    )
    def test_sentinel_does_not_escape(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_coalesce_left_operand(self) -> None:
        # `??` fires only on *undefined*; the sentinel used to be seen as a
        # present value, so the fallback never ran.
        expr = ('( $z := function($x){$x.nope}; '
                '$wrap := function($n){ $z($n) ?? "fallback" }; $wrap(3) )')
        assert ev(expr) == "fallback"

    def test_tco_still_flattens_deep_tail_recursion(self) -> None:
        # The barrier must not disable TCO where it is legitimate: this
        # recursion is 5000 deep and only terminates without a RecursionError
        # if the trampoline is still in play.
        expr = ("( $count := function($n, $acc) { $n = 0 ? $acc "
                ": $count($n - 1, $acc + 1) }; $count(5000, 0) )")
        assert ev(expr) == 5000


class TestPy2OptimizerRebuild:
    """PY-2: the optimizer rebuilt nodes with positional constructors, so any
    fold inside a node reset its non-default dataclass fields."""

    def test_lambda_signature_survives_a_fold_in_the_body(self) -> None:
        # The `("" & "a")` fold rebuilt the Lambda and dropped `<s>`, so the
        # signature check never ran and `$f(5)` returned "5a".
        assert code('( $f := function($x)<s>{ $x & ("" & "a") }; $f(5) )') == "T0410"

    def test_lambda_signature_without_a_fold_still_checks(self) -> None:
        assert code('( $f := function($x)<s>{ $x }; $f(5) )') == "T0410"

    def test_function_call_is_variable_survives_a_fold_in_an_argument(self) -> None:
        # The `1+1` fold rebuilt the FunctionCall and reset is_variable, so
        # `g` was looked up as a built-in and reported as undefined.
        assert ev('( $o := {"g": function($v){$v*10}}; $o.g(1+1) )') == 20

    def test_array_constructor_path_head_survives(self) -> None:
        assert ev("[1,2].[$ + 0]") == [[1], [2]]


class TestPy3FalseConditionalFold:
    """PY-3: `false ? x` / `null ? x` with no else-branch folded to JSON null;
    the reference yields *undefined*."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("false ? 1", MISSING),
            ("null ? 1", MISSING),
            ("[false ? 1]", []),
            ("[null ? 1]", []),
            ('{"a": false ? 1}', {}),
            # with an else-branch the fold is still correct
            ("false ? 1 : 2", 2),
            ("null ? 1 : 2", 2),
            ("true ? 1 : 2", 1),
            ("true ? 1", 1),
        ],
    )
    def test_fold(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestPy12NonFiniteFold:
    """PY-12: `/` and `%` were folded without the isfinite() guard the other
    arithmetic operators have, and `repr(inf)` emitted the bare name `inf`
    into the generated module."""

    def test_overflowing_division_is_not_folded_to_a_bare_name(self) -> None:
        # Used to raise "name 'inf' is not defined" from the generated source.
        result = ev("1e308 / 1e-308")
        assert result == float("inf")

    def test_division_that_stays_finite_is_still_folded(self) -> None:
        assert ev("10 / 4") == 2.5

    def test_modulo_still_folded(self) -> None:
        assert ev("10 % 3") == 1
