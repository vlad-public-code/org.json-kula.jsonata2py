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


class TestPy4DescendingSort:
    """PY-4: `^(>key)` was compiled as fn_reverse(fn_sort(...)). Reversing a
    stable ascending sort swaps tied elements and moves missing keys to the
    front; the reference negates the comparator *after* its undefined checks
    have already short-circuited, so ties keep input order and a missing key
    sorts last in both directions."""

    @pytest.mark.parametrize(
        ("expr", "data", "expected"),
        [
            # tied keys keep input order
            ("items^(>a).n",
             {"items": [{"a": 1, "n": "x"}, {"a": 1, "n": "y"}]},
             ["x", "y"]),
            ("items^(>a).n",
             {"items": [{"a": "b", "n": "x"}, {"a": "a", "n": "y"}, {"a": "b", "n": "z"}]},
             ["x", "z", "y"]),
            # secondary key is applied within the descending primary groups
            ("items^(>a, b).b",
             {"items": [{"a": 1, "b": 1}, {"a": 1, "b": 2}, {"a": 2, "b": 5}]},
             [5, 1, 2]),
            ("items^(<a, >b).n",
             {"items": [{"a": 1, "b": 1, "n": "p"}, {"a": 1, "b": 2, "n": "q"},
                        {"a": 1, "b": 2, "n": "r"}]},
             ["q", "r", "p"]),
            # a missing key sorts last descending, exactly as ascending
            ("items^(>a).n",
             {"items": [{"a": 1, "n": "x"}, {"n": "m"}, {"a": 2, "n": "z"}, {"n": "m2"}]},
             ["z", "x", "m", "m2"]),
            ("items^(a).n",
             {"items": [{"a": 1, "n": "x"}, {"n": "m"}, {"a": 2, "n": "z"}, {"n": "m2"}]},
             ["x", "z", "m", "m2"]),
            ("items^(>$.a)", {"items": [{"a": 2}, {"a": 1}]}, [{"a": 2}, {"a": 1}]),
        ],
    )
    def test_order(self, expr: str, data: object, expected: object) -> None:
        assert ev(expr, data) == expected

    def test_descending_over_a_tuple_stream(self) -> None:
        # sort_tuples took the same reversed-ascending shortcut.
        data = {"items": [{"a": 1, "n": "x"}, {"a": 1, "n": "y"}]}
        assert ev("items@$e^(>$e.a).($e.n)", data) == ["x", "y"]

    def test_sort_builtin_is_unaffected(self) -> None:
        assert ev("$sort([3,1,2])") == [1, 2, 3]


class TestPy10PadAllocation:
    """PY-10 / M-1: the pad width comes straight from input data, and $pad
    built a string of that length -- `$pad("a", 1e15)` grew the process to
    10.7 GB. The reference throws a host RangeError ("Invalid array length"),
    which is not a JSONata error at all, so a JSONata error is raised here."""

    @pytest.mark.parametrize("width", [1e15, -1e15, 2**40])
    def test_huge_width_raises_instead_of_allocating(self, width: float) -> None:
        assert code(f"$pad('a', {width!r})") == "D1001"

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("$pad('a', 5)", "a    "),
            ("$pad('a', -5, 'xy')", "xyxya"),
            ("$pad('foo', 8, '-+')", "foo-+-+-"),
            ("$pad('a', 0)", "a"),
            ("$pad('abc', 2)", "abc"),
            ("$pad('', 3, 'ab')", "aba"),
        ],
    )
    def test_normal_widths_unchanged(self, expr: str, expected: str) -> None:
        assert ev(expr) == expected


class TestPy11GroupByScaling:
    """PY-11 / M-2: a group-by bucket was accumulated with
    `grp[k] = fn_append(grp[k], elem)`, and fn_append copies the whole
    accumulated list -- O(n^2) time and allocation in the bucket size."""

    @pytest.mark.parametrize(
        ("expr", "data", "expected"),
        [
            # fn_append's flatten-one-level rule must survive the rewrite
            ("a{b: c}", {"a": [{"b": "x", "c": [1, 2]}, {"b": "x", "c": [3]}]},
             {"x": [1, 2, 3]}),
            # ...including "a lone item is stored verbatim"
            ("a{b: c}", {"a": [{"b": "x", "c": [3]}]}, {"x": [3]}),
            ("a{b: c}", {"a": [{"b": "x", "c": 1}, {"b": "x", "c": 2},
                               {"b": "y", "c": 3}]}, {"x": [1, 2], "y": 3}),
            ("a{b: $sum(c)}", {"a": [{"b": "x", "c": 1}, {"b": "x", "c": 2},
                                     {"b": "y", "c": 3}]}, {"x": 3, "y": 3}),
            ("nope{'k':1}", None, {"k": 1}),
            ("nope{'k':$string($)}", None, {}),
        ],
    )
    def test_semantics_unchanged(self, expr: str, data: object, expected: object) -> None:
        assert ev(expr, data) == expected

    def test_one_large_bucket_scales_linearly(self) -> None:
        import time

        expr = jsonata.JsonataExpressionFactory().compile("a{b: c}")

        def timed(n: int) -> float:
            data = {"a": [{"b": "x", "c": i} for i in range(n)]}
            expr.evaluate(data)  # warm up
            runs = []
            for _ in range(5):
                start = time.perf_counter()
                expr.evaluate(data)
                runs.append(time.perf_counter() - start)
            return min(runs)

        small = timed(20_000)
        large = timed(80_000)
        # 4x the items. Linear gives ~4x the time (measured 3.8-5.7x);
        # the quadratic accumulation gave ~16x.
        assert large < small * 9, f"20k={small:.4f}s 80k={large:.4f}s"


class TestPy5PackedArguments:
    """PY-5: a lambda body unpacked its parameters whenever it was applied to
    a `list`, so a multi-parameter lambda called with ONE array argument had
    that array spread across its parameters."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ('( $f := function($arr, $sep){ $join($arr, $sep) }; $f(["a","b"]) )', "ab"),
            ('( $f := function($a,$b){ $a }; $f([1,2]) )', [1, 2]),
            ('( $f := function($a,$b,$c){ [$a,$b,$c] }; $f([1,2,3]) )', [1, 2, 3]),
            # piping an array into a 2-parameter function is the same shape
            ('( $f := function($arr,$sep){ $join($arr,$sep) }; ["a","b"] ~> $f() )', "ab"),
            # genuine multi-argument calls still unpack
            ('( $f := function($a,$b){ [$a,$b] }; $f([1,2],[3,4]) )', [1, 2, 3, 4]),
            ('( $f := function($a,$b){ $b }; $f([1,2], 9) )', 9),
            ('( $f := function($a,$b){ [$a,$b] }; $f(1) )', [1]),
            ('( $f := function($a){ $a }; $f([1,2]) )', [1, 2]),
            # built-in callbacks still receive [value, index, array]
            ("$map([[1,2],[3,4]], function($v,$i){ [$v,$i] })", [[1, 2, 0], [3, 4, 1]]),
            ('( $f := function($a,$b){ [$a,$b] }; $map([[1,2]], $f) )', [1, 2, 0]),
            ('$sift({"a":1}, function($v,$k){ $k = "a" })', {"a": 1}),
            ('$each({"a":1}, function($v,$k){ $k })', "a"),
            ("$reduce([[1],[2]], function($a,$b){ $append($a,$b) })", [1, 2]),
        ],
    )
    def test_argument_packing(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_pack_args_produces_the_marker_type(self) -> None:
        from jsonata2py.runtime.core import pack_args
        from jsonata2py.runtime.values import PackedArgs

        assert isinstance(pack_args(1, 2), PackedArgs)
        assert not isinstance([1, 2], PackedArgs)


class TestPy6Eval:
    """PY-6: `$eval` compiled the text into a fresh expression evaluated with
    no bindings, its own recursion budget and no deadline. The reference
    evaluates it in the environment of the `$eval` call."""

    @pytest.mark.parametrize(
        ("expr", "data", "expected"),
        [
            ('( $x := 5; $eval("$x + 1") )', None, 6),
            ('( $x := 5; $eval("$x + 1", {"a":1}) )', None, 6),
            # innermost binding wins
            ('( $x := 5; ( $x := 9; $eval("$x") ) )', None, 9),
            # a local *function* is callable from the nested expression
            ('( $g := function($n){$n*3}; $eval("$g(2)") )', None, 6),
            # a local bound inside a path block is visible too
            ('a.( $x := b; $eval("$x") )', {"a": {"b": 7}}, 7),
            # the nested frame's own locals do not escape
            ('( $x := 1; $eval("($x := 2; $x)") + $x )', None, 3),
            ('$eval("$sum([1,2])")', None, 3),
            ('$eval("$foo")', None, MISSING),
            ('$eval("$")', {"a": 1}, {"a": 1}),
        ],
    )
    def test_scope(self, expr: str, data: object, expected: object) -> None:
        assert ev(expr, data) == expected

    def test_per_evaluation_value_bindings_are_visible(self) -> None:
        b = jsonata.JsonataBindings().bind_value("y", 41)
        expr = jsonata.JsonataExpressionFactory().compile('$eval("$y + 1")')
        assert expr.evaluate(None, b) == 42

    def test_per_evaluation_function_bindings_are_visible(self) -> None:
        b = jsonata.JsonataBindings().bind_function("dbl", lambda a: a * 2)
        expr = jsonata.JsonataExpressionFactory().compile('$eval("$dbl(4)")')
        assert expr.evaluate(None, b) == 8

    def test_nested_eval_cannot_escape_the_outer_timeout(self) -> None:
        # The nested expression has no timeout of its own, so before the
        # deadline was inherited this ran to completion in 637 ms despite a
        # 20 ms budget. (The reference leaks its timeout here too; the three
        # ports deliberately close the hole rather than reproduce it.)
        import time

        expr = jsonata.JsonataExpressionFactory().compile('$eval("$sum([1..3000000])")')
        expr.set_timeout(20)
        start = time.perf_counter()
        with pytest.raises(JsonataEvaluationError):
            expr.evaluate(None)
        assert time.perf_counter() - start < 1.0

    def test_nested_eval_shares_the_recursion_budget(self) -> None:
        # Recursing through $eval used to get a fresh call-depth counter
        # each time, so the U1001 limit never fired.
        expr = ('( $f := function($n){ $n = 0 ? 0 '
                ': $eval("$f(" & $string($n-1) & ")") }; $f(500) )')
        with pytest.raises(JsonataEvaluationError):
            ev(expr)
