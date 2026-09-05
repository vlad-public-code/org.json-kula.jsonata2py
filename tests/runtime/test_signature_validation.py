"""Built-in argument-signature validation (§17 of the cross-port notes).

The reference validates every argument against the built-in's `<...>`
signature and reports a mismatch as T0410 (or T0412 for an array whose
*elements* are wrong). This port used to validate only when the argument
count under-filled the parameters -- the case that needs context
substitution -- so a fully supplied call was never type-checked at all.

Turning that on naively costs 75% of evaluation throughput, because the
runtime path builds a symbol string and runs a regex per call, 36 times per
evaluation of the benchmark expression. What is here instead: the assignment
of arguments to parameters is settled at *compile* time whenever it cannot
depend on the argument types, and only the positions that restrict something
get an inline wrapper. That costs ~1% (measured, paired, best-of-3).

Expectations come from the reference at `c:/vlad-projects/js/jsonata` (2.2.2).
"""

from __future__ import annotations

from typing import ClassVar

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.core import fn_append, fn_count, fn_reverse, fn_sort, fn_zip
from jsonata2py.runtime.signature import arg_check_specs, arity_bounds, sig_a
from jsonata2py.runtime.values import MISSING
from jsonata2py.translator.translator import _ARRAY_COERCION_IS_REDUNDANT, _BUILTIN_SIGNATURES

DATA = {"nums": [1, 2, 3], "o": {"a": 1}, "s": "abc", "one": [7]}


def ev(expr: str, data: object = DATA) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


def code(expr: str, data: object = DATA) -> str:
    with pytest.raises(jsonata.JsonataError) as exc:
        ev(expr, data)
    return str(exc.value.error_code)


class TestAWrongArgumentTypeIsT0410:
    @pytest.mark.parametrize(
        "expr",
        [
            '$sift(nums, 1)',
            '$each(o, 1)',
            '$sort("abc", 1)',
            '$lookup(nums, 1)',
            '$map(nums, 1)',
            '$filter(nums, "a")',
            '$reduce(nums, nums)',
            '$substringBefore(1, "-")',
            '$base64decode(1)',
            '$eval(1)',
            '$formatBase("a", 16)',
            '$pad(1, 5)',
        ],
    )
    def test_t0410(self, expr: str) -> None:
        assert code(expr) == "T0410"

    @pytest.mark.parametrize(
        "expr",
        ["$millis(nums, nums)", "$error('a', nums)", "$each(nope)", "$number(1, 2)"],
    )
    def test_an_impossible_argument_count_is_also_t0410(self, expr: str) -> None:
        """A count the signature cannot accept is a signature mismatch like
        any other, not the built-in failing its own way."""
        assert code(expr) == "T0410"

    def test_a_wrong_element_type_is_t0412(self) -> None:
        assert code("$sum(['a'])") == "T0412"
        assert code("$join([1,2])") == "T0412"

    def test_a_later_t0410_outranks_an_earlier_t0412(self) -> None:
        """The reference matches the whole argument list against the
        signature before it looks at any array's element types."""
        assert code("$join(nums, nums)") == "T0410"


class TestValidArgumentsAreUntouched:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("$sum(nums)", 6),
            ("$count(nums)", 3),
            ("$count('a')", 1),
            ("$sum(1)", 1),
            ("$string(o)", '{"a":1}'),
            ("$sort(nums)", [1, 2, 3]),
            ("$map(nums, function($v){$v})", [1, 2, 3]),
            ("$join(['a','b'], '-')", "a-b"),
            ("$round(1.234, 2)", 1.23),
            ("$substring('hello', 1, 2)", "el"),
        ],
    )
    def test_ordinary_calls(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_an_absent_argument_still_passes_through(self) -> None:
        assert ev("$sum(nope)") is MISSING
        assert ev("$count(nope)") == 0


class TestBuiltInsAsFirstClassValues:
    """Every built-in the reference registers is a function value there. A
    hand-maintained wrapper table left the rest resolving to nothing, so
    `$abs` was absent and passing it anywhere silently did nothing."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("$map([-1,2], $abs)", [1, 2]),
            ("$map([1,2], $string)", ["1", "2"]),
            ("1 ~> $abs", 1),
            ("-1 ~> $string", "-1"),
            ("[1,2] ~> $sum", 3),
            ("$filter([1,0,2], $boolean)", [1, 2]),
            ("$map(['a','b'], $uppercase)", ["A", "B"]),
        ],
    )
    def test_a_builtin_is_a_value(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_every_signature_resolves_to_something_callable(self) -> None:
        """The value table is built from the signature table, so a built-in
        cannot be in one and missing from the other."""
        from jsonata2py.runtime.core import builtin_value

        missing = [n for n in _BUILTIN_SIGNATURES if builtin_value(n) is MISSING]
        assert missing == [], f"no first-class value for: {missing}"


class TestUnaryMinusComposesWithChaining:
    """`-` binds tighter than `~>` in the reference, and the negative-literal
    shortcut used to return before the chain level ever ran -- so
    `-1 ~> $string` did not parse at all."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [("-1 ~> $string", "-1"), ("-1.5 ~> $string", "-1.5"), ("-1[0]", -1), ("-1 & 'a'", "-1a")],
    )
    def test_negative_literals(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestTheRedundantCoercionListIsTrue:
    """`_ARRAY_COERCION_IS_REDUNDANT` lets the translator drop the wrapper on
    an untyped array parameter -- 21 of the benchmark's 49 wrapped arguments.
    It is a claim about each implementation, so it is re-derived here rather
    than trusted: an implementation that stops coercing fails this test
    instead of silently changing an answer."""

    _XS: ClassVar[list[object]] = [[1, 2, 3], [], "abc", 1, 0, True, None, {"a": 1}, MISSING, [[1], [2]]]
    _FNS: ClassVar[dict[str, object]] = {
        "count": fn_count,
        "reverse": fn_reverse,
        "zip": fn_zip,
        "sort": fn_sort,
        "append": fn_append,
    }

    @pytest.mark.parametrize("name", sorted(_ARRAY_COERCION_IS_REDUNDANT))
    def test_the_builtin_applies_the_coercion_itself(self, name: str) -> None:
        fn = self._FNS[name]
        for x in self._XS:
            direct = _outcome(fn, x)
            wrapped = _outcome(fn, sig_a(x, "", 1))
            assert direct == wrapped, f"${name} does not coerce {x!r} itself: {direct} vs {wrapped}"


def _outcome(fn: object, x: object) -> object:
    try:
        return repr(fn(x))  # type: ignore[operator]
    except Exception as e:  # the comparison is the point
        return f"ERR {type(e).__name__}"


class TestThePlanIsOnlyUsedWhereItIsExact:
    def test_a_context_fillable_parameter_declines_the_inline_plan(self) -> None:
        """A trailing `-` parameter can take the evaluation context, which
        shifts the assignment -- the one thing the plan cannot describe."""
        assert arg_check_specs("<x-b?:s>", 0) is None

    def test_arity_bounds(self) -> None:
        assert arity_bounds("<:n>") == (0, 0)
        assert arity_bounds("<a<n>:n>") == (1, 1)
        assert arity_bounds("<s-s?:n>") == (0, 2)
        assert arity_bounds("<a+>") == (0, -1)
