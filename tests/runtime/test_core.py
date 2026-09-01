"""Phase 3 gate: runtime core functions tested directly, with no translator
involved.
"""

from __future__ import annotations

import pytest

from jsonata2py.errors import _RuntimeEvaluationError as RuntimeEvaluationError
from jsonata2py.runtime import core as rt
from jsonata2py.runtime.values import MISSING


class TestValueModelTraps:
    """D1: bool-is-int-subclass and None-vs-MISSING traps."""

    def test_bool_and_int_never_conflate_in_equality(self) -> None:
        assert rt.eq(True, 1) is False
        assert rt.eq(False, 0) is False
        assert rt.eq(1, 1) is True
        assert rt.eq(True, True) is True

    def test_is_number_excludes_bool(self) -> None:
        assert rt.is_number(1) is True
        assert rt.is_number(1.5) is True
        assert rt.is_number(True) is False
        assert rt.is_number(False) is False

    def test_none_is_null_not_missing(self) -> None:
        assert rt.missing(None) is False
        assert rt.missing(MISSING) is True
        assert rt.fn_type(None) == "null"
        assert rt.fn_type(MISSING) is MISSING

    def test_deep_equals_switches_on_kind_first(self) -> None:
        assert rt.deep_equals(1, 1.0) is True
        assert rt.deep_equals(1, "1") is False
        assert rt.deep_equals(True, 1) is False
        assert rt.deep_equals(None, None) is True
        assert rt.deep_equals([1, 2], [1, 2]) is True
        assert rt.deep_equals({"a": 1}, {"a": 1}) is True
        assert rt.deep_equals({"a": 1}, {"a": 1, "b": 2}) is False


class TestPathNavigation:
    def test_field_on_object(self) -> None:
        assert rt.field({"a": 1}, "a") == 1
        assert rt.field({"a": 1}, "b") is MISSING

    def test_field_on_array_maps_and_unwraps_singleton(self) -> None:
        assert rt.field([{"a": 1}, {"a": 2}], "a") == [1, 2]
        assert rt.field([{"a": 1}, {"b": 2}], "a") == 1  # singleton unwraps

    def test_field_on_missing_or_none(self) -> None:
        assert rt.field(MISSING, "a") is MISSING
        assert rt.field(None, "a") is MISSING

    def test_wildcard(self) -> None:
        assert sorted(rt.wildcard({"a": 1, "b": 2})) == [1, 2]

    def test_descendant(self) -> None:
        result = rt.descendant({"a": {"b": 1}})
        assert {"b": 1} in result if isinstance(result, list) else result == {"b": 1}

    def test_subscript_negative_index(self) -> None:
        assert rt.subscript([1, 2, 3], -1) == 3
        assert rt.subscript([1, 2, 3], 0) == 1
        assert rt.subscript([1, 2, 3], 5) is MISSING

    def test_force_array(self) -> None:
        assert rt.force_array(1) == [1]
        assert rt.force_array([1, 2]) == [1, 2]
        assert rt.force_array(MISSING) is MISSING


class TestArithmetic:
    def test_basic_ops(self) -> None:
        assert rt.add(1, 2) == 3
        assert rt.subtract(5, 3) == 2
        assert rt.multiply(3, 4) == 12
        assert rt.divide(10, 2) == 5
        assert rt.modulo(10, 3) == 1

    def test_missing_propagates(self) -> None:
        assert rt.add(MISSING, 1) is MISSING
        assert rt.add(1, MISSING) is MISSING

    def test_non_numeric_raises_t2001_t2002(self) -> None:
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.add("x", 1)
        assert e.value.error_code == "T2001"
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.add(1, "x")
        assert e.value.error_code == "T2002"

    def test_divide_by_zero_is_infinity_not_error(self) -> None:
        assert rt.divide(1, 0) == float("inf")
        assert rt.divide(-1, 0) == float("-inf")

    def test_modulo_by_zero_raises(self) -> None:
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.modulo(1, 0)
        assert e.value.error_code == "D1001"

    def test_num_node_int_vs_float(self) -> None:
        assert rt.num_node(3.0) == 3
        assert isinstance(rt.num_node(3.0), int)
        assert rt.num_node(3.5) == 3.5
        assert isinstance(rt.num_node(3.5), float)


class TestComparisons:
    def test_ordering(self) -> None:
        assert rt.lt(1, 2) is True
        assert rt.gt(2, 1) is True
        assert rt.le(1, 1) is True
        assert rt.ge(1, 1) is True

    def test_string_ordering(self) -> None:
        assert rt.lt("a", "b") is True

    def test_incompatible_types_raise_t2009_or_t2010(self) -> None:
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.lt(1, "a")
        assert e.value.error_code == "T2009"
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.lt(1, None)
        assert e.value.error_code == "T2010"


class TestBooleanLogic:
    def test_is_truthy_rules(self) -> None:
        assert rt.is_truthy(0) is False
        assert rt.is_truthy("") is False
        assert rt.is_truthy(None) is False
        assert rt.is_truthy(MISSING) is False
        assert rt.is_truthy({}) is False
        assert rt.is_truthy([]) is False
        assert rt.is_truthy([0, False, ""]) is False
        assert rt.is_truthy([0, 1]) is True
        assert rt.is_truthy("x") is True
        assert rt.is_truthy(1) is True

    def test_elvis_and_coalesce(self) -> None:
        assert rt.elvis(0, "default") == "default"
        assert rt.elvis("x", "default") == "x"
        assert rt.coalesce(MISSING, "default") == "default"
        assert rt.coalesce(None, "default") is None  # null is defined

    def test_and_or_short_circuit(self) -> None:
        calls = []
        rt.and_(False, lambda: calls.append(1) or True)
        assert calls == []  # short-circuited
        rt.or_(True, lambda: calls.append(1) or True)
        assert calls == []


class TestConstructorsAndAggregates:
    def test_array_of_flattens_and_preserves(self) -> None:
        assert rt.array_of([1, 2], 3) == [1, 2, 3]
        preserved = rt.preserve_array([1, 2])
        assert rt.array_of(preserved, 3) == [[1, 2], 3]

    def test_range(self) -> None:
        assert rt.range_(1, 5) == [1, 2, 3, 4, 5]

    def test_object_duplicate_key_raises_d1009(self) -> None:
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.object_("a", 1, "a", 2)
        assert e.value.error_code == "D1009"

    def test_fused_aggregates_match_unfused(self) -> None:
        data = [{"price": 10}, {"price": 20}, {"price": 30}]
        assert rt.fn_sum_field(data, "price") == 60
        assert rt.fn_sum([d["price"] for d in data]) == 60
        assert rt.fn_average_field(data, "price") == 20
        assert rt.fn_max_field(data, "price") == 30
        assert rt.fn_min_field(data, "price") == 10

    def test_fn_count_field_eq(self) -> None:
        data = [{"a": "x"}, {"a": "y"}, {"a": "x"}]
        assert rt.fn_count_field_eq(data, "a", "x") == 2


class TestSequenceHOF:
    def test_fn_map_plain_callable(self) -> None:
        assert rt.fn_map([1, 2, 3], lambda x: x * 2) == [2, 4, 6]

    def test_fn_filter(self) -> None:
        assert rt.fn_filter([1, 2, 3, 4], lambda x: x % 2 == 0) == [2, 4]

    def test_fn_reduce(self) -> None:
        assert rt.fn_reduce([1, 2, 3, 4], lambda t: t[0] + t[1], MISSING) == 10

    def test_fn_sort_scalar(self) -> None:
        assert rt.fn_sort([3, 1, 2]) == [1, 2, 3]

    def test_fn_sort_mixed_types_raises_d3070(self) -> None:
        """`$sort` with no comparator reports D3070, verified against the
        reference interpreter ($sort([1,"a"]) -> D3070). This asserted
        T2007 before, which is the code the `^(key)` order-by operator
        reports about its key expression -- see the test below."""
        from jsonata2py.runtime import sequences as seq

        with pytest.raises(RuntimeEvaluationError) as e:
            seq.fn_sort([1, "a"], None)
        assert e.value.error_code == "D3070"

        for bad in ([True, False], [None, 1]):
            with pytest.raises(RuntimeEvaluationError) as e:
                seq.fn_sort(bad, None)
            assert e.value.error_code == "D3070"

    def test_order_by_key_still_reports_t2007_t2008(self) -> None:
        """The order-by operator keeps its own codes: mixed key types are
        T2007 and a non-string/number key is T2008."""
        from jsonata2py.runtime import sequences as seq

        with pytest.raises(RuntimeEvaluationError) as e:
            seq.fn_sort([{"k": 1}, {"k": "a"}], lambda item: item["k"])
        assert e.value.error_code == "T2007"

        with pytest.raises(RuntimeEvaluationError) as e:
            seq.fn_sort([{"k": True}], lambda item: item["k"])
        assert e.value.error_code == "T2008"


class TestLambdaValuesAndApply:
    def test_fn_apply_basic(self) -> None:
        f = rt.lambda_value(lambda x: x + 1)
        assert rt.fn_apply(f, 5) == 6

    def test_fn_apply_on_non_function_raises_t1006(self) -> None:
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.fn_apply(42, 1)
        assert e.value.error_code == "T1006"

    def test_fn_pipe_composes_functions(self) -> None:
        f = rt.lambda_value(lambda x: x + 1)
        g = rt.lambda_value(lambda x: x * 2)
        composed = rt.fn_pipe(f, g)
        assert rt.fn_apply(composed, 5) == 12  # (5+1)*2

    def test_fn_pipe_value_piping(self) -> None:
        g = rt.lambda_value(lambda x: x * 2)
        assert rt.fn_pipe(5, g) == 10

    def test_recursive_apply_within_call_depth_budget(self) -> None:
        def make_fact():
            def fact(n: int) -> int:
                if n <= 1:
                    return 1
                return n * rt.fn_apply(rt.lambda_value(fact), n - 1)

            return fact

        f = rt.lambda_value(make_fact())
        assert rt.fn_apply(f, 10) == 3628800

    def test_call_depth_exceeded_raises_u1001(self) -> None:
        def make_fact():
            def fact(n: int):
                return rt.fn_apply(rt.lambda_value(fact), n + 1)

            return fact

        f = rt.lambda_value(make_fact())
        with pytest.raises(RuntimeEvaluationError) as e:
            rt.fn_apply(f, 0)
        assert e.value.error_code == "U1001"


class TestNumberFormatting:
    def test_integer_no_decimal_point(self) -> None:
        assert rt.to_text(3) == "3"
        assert rt.number_to_string(3.0) == "3"

    def test_fractional(self) -> None:
        assert rt.to_text(3.14) == "3.14"

    def test_large_number_scientific_notation(self) -> None:
        s = rt.to_text(1e21)
        assert "e+" in s

    def test_container_to_text_uses_jsonata_number_formatting(self) -> None:
        assert rt.to_text([1, 2.5]) == "[1,2.5]"
        assert rt.to_text({"a": 1}) == '{"a":1}'


class TestNumNodeAcceptsInt:
    """num_node is annotated `float`, but int is assignable to a float
    parameter, so callers pass ints and no type checker objects.

    That matters because int.is_integer() only exists on CPython 3.12+. On
    the declared 3.11 floor an int argument raised AttributeError, which
    showed up as 60 failures across $parseInteger and $number.
    """

    def test_int_argument_is_returned_as_int(self) -> None:
        assert rt.num_node(5) == 5
        assert isinstance(rt.num_node(5), int)

    def test_int_argument_out_of_int64_range_is_preserved(self) -> None:
        huge = 2**70
        assert rt.num_node(huge) == huge

    def test_float_argument_still_narrows_to_int_when_whole(self) -> None:
        assert isinstance(rt.num_node(5.0), int)
        assert rt.num_node(5.5) == 5.5
        assert isinstance(rt.num_node(5.5), float)

    def test_non_finite_floats_stay_float(self) -> None:
        assert rt.num_node(float("inf")) == float("inf")
        assert rt.num_node(float("nan")) != rt.num_node(float("nan"))
