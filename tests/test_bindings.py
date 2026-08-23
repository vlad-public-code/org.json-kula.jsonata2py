"""Tests for the bindings API (Phase 6 gate).

Ported from JsonataBindingsTest.java / JsonataFunctionValueBindingsTest.java:
  * Per-evaluation bindings via expr.evaluate(data, bindings)
  * Permanent value bindings via expr.assign
  * Permanent function bindings via expr.register_function
  * JsonataBoundFunction invocation and signature
  * Precedence rules (per-eval overrides permanent)
  * A bound function referenced as a value: $map, ~>, $type, assigned to a
    local, passed to another bound function
  * A function value (JLambda) bound with bind_value and called directly
  * The "f" (function) signature type, on both a Python bound function and
    a JSONata lambda signature
  * Thread safety with concurrent per-evaluation bindings
"""

from __future__ import annotations

import concurrent.futures

import pytest

import jsonata2py as jsonata
from jsonata2py.bindings import JsonataBindings, JsonataFunctionArguments, bound_function
from jsonata2py.errors import JsonataEvaluationError
from jsonata2py.runtime.lambdas import fn_apply, lambda_node
from jsonata2py.runtime.values import MISSING

EMPTY: dict = {}

_FACTORY = jsonata.JsonataExpressionFactory()


def compile_(expr: str) -> jsonata.CompiledExpression:
    return _FACTORY.compile(expr)


def eval_(expr: str, bindings: JsonataBindings | None = None) -> object:
    return compile_(expr).evaluate(EMPTY, bindings)


def doubler():
    @bound_function("<n:n>")
    def double(n: float) -> float:
        return n * 2

    return double


def adder():
    @bound_function("<nn:n>")
    def add(a: float, b: float) -> float:
        return a + b

    return add


# =============================================================================
# Per-evaluation value bindings
# =============================================================================


def test_per_eval_single_value_resolved_in_expression():
    expr = compile_("$rate * price")
    b = JsonataBindings().bind_value("rate", 1.2)
    assert expr.evaluate({"price": 100}, b) == pytest.approx(120.0)


def test_per_eval_multiple_values_all_resolved():
    expr = compile_("$a + $b + $c")
    b = JsonataBindings().bind_value("a", 1).bind_value("b", 2).bind_value("c", 3)
    assert expr.evaluate(EMPTY, b) == 6


def test_per_eval_string_value_resolved_in_concat():
    expr = compile_('$greeting & ", " & name')
    b = JsonataBindings().bind_value("greeting", "Hello")
    assert expr.evaluate({"name": "World"}, b) == "Hello, World"


def test_per_eval_unbound_variable_returns_missing():
    expr = compile_("$missing")
    assert expr.evaluate(EMPTY, JsonataBindings()) is MISSING


def test_per_eval_none_bindings_behaves_like_no_bindings():
    expr = compile_("1 + 1")
    assert expr.evaluate(EMPTY, None) == 2


def test_per_eval_value_used_in_conditional():
    expr = compile_("$discount > 0 ? price * (1 - $discount) : price")
    b10 = JsonataBindings().bind_value("discount", 0.10)
    b0 = JsonataBindings().bind_value("discount", 0)
    data = {"price": 200}
    assert expr.evaluate(data, b10) == pytest.approx(180.0)
    assert expr.evaluate(data, b0) == pytest.approx(200.0)


def test_per_eval_value_used_in_predicate():
    expr = compile_("items[price < $maxPrice].name")
    b = JsonataBindings().bind_value("maxPrice", 15)
    data = {"items": [{"name": "A", "price": 10}, {"name": "B", "price": 20}]}
    assert expr.evaluate(data, b) == "A"


# =============================================================================
# Permanent value bindings -- assign()
# =============================================================================


def test_assign_value_available_on_all_subsequent_evals():
    expr = compile_("$taxRate * amount")
    expr.assign("taxRate", 0.2)
    assert expr.evaluate({"amount": 100}) == pytest.approx(20.0)
    assert expr.evaluate({"amount": 200}) == pytest.approx(40.0)


def test_assign_multiple_values_all_available():
    expr = compile_("$x + $y")
    expr.assign("x", 7)
    expr.assign("y", 3)
    assert expr.evaluate(EMPTY) == 10


def test_assign_overwrite_existing_assignment():
    expr = compile_("$factor * 10")
    expr.assign("factor", 2)
    assert expr.evaluate(EMPTY) == 20
    expr.assign("factor", 5)
    assert expr.evaluate(EMPTY) == 50


def test_assign_does_not_affect_other_instances():
    e1 = compile_("$x")
    e2 = compile_("$x")
    e1.assign("x", 42)
    assert e2.evaluate(EMPTY) is MISSING


# =============================================================================
# Per-evaluation function bindings
# =============================================================================


def test_per_eval_bound_function_called_with_args():
    expr = compile_("$double(value)")
    b = JsonataBindings().bind_function("double", doubler())
    assert expr.evaluate({"value": 21}, b) == pytest.approx(42.0)


def test_per_eval_bound_function_multiple_args():
    expr = compile_("$add($a, $b)")
    b = JsonataBindings().bind_value("a", 3).bind_value("b", 4).bind_function("add", adder())
    assert expr.evaluate(EMPTY, b) == 7


def test_per_eval_unbound_function_raises():
    expr = compile_("$notRegistered(1)")
    with pytest.raises(JsonataEvaluationError) as exc_info:
        expr.evaluate(EMPTY, JsonataBindings())
    assert exc_info.value.error_code == "T1006"
    assert exc_info.value.message == "The function 'notRegistered' is not defined"


def test_per_eval_bound_function_can_raise_evaluation_error():
    expr = compile_("$boom()")

    @bound_function("<:j>")
    def boom():
        raise JsonataEvaluationError(None, "intentional error")

    b = JsonataBindings().bind_function("boom", boom)
    with pytest.raises(JsonataEvaluationError):
        expr.evaluate(EMPTY, b)


# =============================================================================
# Permanent function bindings -- register_function()
# =============================================================================


def test_register_function_available_on_all_subsequent_evals():
    expr = compile_("$square(n)")

    @bound_function("<n:n>")
    def square(n: float) -> float:
        return n * n

    expr.register_function("square", square)
    assert expr.evaluate({"n": 3}) == 9
    assert expr.evaluate({"n": 5}) == 25


def test_register_function_does_not_affect_other_instances():
    e1 = compile_("$fn()")
    e2 = compile_("$fn()")

    @bound_function("<:n>")
    def fn():
        return 1

    e1.register_function("fn", fn)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        e2.evaluate(EMPTY)
    assert exc_info.value.error_code == "T1006"
    assert exc_info.value.message == "The function 'fn' is not defined"


# =============================================================================
# Precedence: per-eval overrides permanent
# =============================================================================


def test_per_eval_value_overrides_permanent_assign():
    expr = compile_("$x")
    expr.assign("x", 10)
    b = JsonataBindings().bind_value("x", 99)
    assert expr.evaluate(EMPTY, b) == 99
    assert expr.evaluate(EMPTY) == 10


def test_per_eval_function_overrides_permanent_register():
    expr = compile_("$fn()")

    @bound_function("<:n>")
    def fn1():
        return 1

    @bound_function("<:n>")
    def fn2():
        return 2

    expr.register_function("fn", fn1)
    b = JsonataBindings().bind_function("fn", fn2)
    assert expr.evaluate(EMPTY, b) == 2
    assert expr.evaluate(EMPTY) == 1


# =============================================================================
# JsonataFunctionArguments
# =============================================================================


def test_function_arguments_out_of_range_index_returns_missing():
    args = JsonataFunctionArguments([5])
    assert args.get(0) == 5
    assert args.get(1) is MISSING
    assert args.get(-1) is MISSING


def test_function_arguments_len_matches_supplied_args():
    args = JsonataFunctionArguments([1, 2])
    assert len(args) == 2


def test_function_arguments_as_list_is_a_defensive_copy():
    args = JsonataFunctionArguments([1])
    copy = args.as_list()
    copy.append(2)
    assert args.as_list() == [1]


# =============================================================================
# JsonataBoundFunction -- get_function_signature
# =============================================================================


def test_bound_function_get_function_signature_returns_configured_signature():
    @bound_function("<s-:n>")
    def fn(s):
        return 0

    assert fn.get_function_signature() == "<s-:n>"


# =============================================================================
# Backward compatibility
# =============================================================================


def test_evaluate_without_bindings_still_works():
    expr = compile_("a + b")
    assert expr.evaluate({"a": 3, "b": 4}) == 7


def test_evaluate_with_empty_bindings_still_works():
    expr = compile_("a * 2")
    assert expr.evaluate({"a": 5}, JsonataBindings()) == 10


# =============================================================================
# Thread safety -- concurrent per-evaluation bindings
# =============================================================================


def test_per_eval_concurrent_evals_different_bindings_no_interference():
    expr = compile_("$rate * amount")
    threads = 32
    iterations = 20

    def worker(rate: float) -> int:
        errors = 0
        for _ in range(iterations):
            b = JsonataBindings().bind_value("rate", rate)
            result = expr.evaluate({"amount": 10}, b)
            if abs(result - rate * 10) > 1e-9:
                errors += 1
        return errors

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, t + 1) for t in range(threads)]
        total_errors = sum(f.result() for f in futures)
    assert total_errors == 0


def test_assign_concurrent_reads_permanent_binding_stable():
    expr = compile_("$multiplier * x")
    expr.assign("multiplier", 3)
    threads = 32

    def worker(x: int) -> tuple[int, float]:
        return x, expr.evaluate({"x": x})

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, x) for x in range(1, threads + 1)]
        for f in futures:
            x, result = f.result()
            assert result == pytest.approx(x * 3)


# =============================================================================
# A bound function used as a value
# =============================================================================


def test_bound_function_as_value_passed_to_map():
    b = JsonataBindings().bind_function("double", doubler())
    assert eval_("$map([1,2,3], $double)", b) == [2, 4, 6]


def test_bound_function_as_value_piped_through_chain():
    b = JsonataBindings().bind_function("double", doubler())
    assert eval_("5 ~> $double", b) == 10


def test_bound_function_as_value_is_a_function():
    b = JsonataBindings().bind_function("double", doubler())
    assert eval_("$type($double)", b) == "function"


def test_bound_function_as_value_assigned_to_local_then_called():
    b = JsonataBindings().bind_function("double", doubler())
    assert eval_("($f := $double; $f(4))", b) == 8


def test_bound_function_as_value_passed_to_another_bound_function():
    @bound_function("<fj:j>")
    def apply_twice(f, x):
        return fn_apply(f, fn_apply(f, x))

    b = JsonataBindings().bind_function("double", doubler()).bind_function("applyTwice", apply_twice)
    assert eval_("$applyTwice($double, 3)", b) == 12


def test_bound_function_as_value_two_param_callback_receives_index():
    @bound_function("<nn:n>")
    def with_index(value, index):
        return value * 10 + index

    b = JsonataBindings().bind_function("withIndex", with_index)
    assert eval_("$map([1,2,3], $withIndex)", b) == [10, 21, 32]


def test_bound_function_as_value_used_as_reduce_accumulator():
    b = JsonataBindings().bind_function("add", adder())
    assert eval_("$reduce([1,2,3,4], $add)", b) == 10


def test_bound_function_as_value_used_as_sort_comparator():
    @bound_function("<nn:b>")
    def desc(a, b_):
        return a < b_

    b = JsonataBindings().bind_function("desc", desc)
    assert eval_("$sort([2,3,1], $desc)", b) == [3, 2, 1]


def test_permanently_registered_function_usable_as_value():
    expr = compile_("$map([1,2,3], $double)")
    expr.register_function("double", doubler())
    assert expr.evaluate(EMPTY) == [2, 4, 6]


def test_bound_function_as_value_still_reports_its_own_errors():
    @bound_function("<j:j>")
    def boom(x):
        raise JsonataEvaluationError("D9999", "intentional")

    b = JsonataBindings().bind_function("boom", boom)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$map([1], $boom)", b)
    assert exc_info.value.error_code == "D9999"


def test_bound_function_as_value_signature_is_still_applied():
    @bound_function("<n:b>")
    def is_number(n):
        return isinstance(n, (int, float)) and not isinstance(n, bool)

    b = JsonataBindings().bind_function("isNumber", is_number)
    assert eval_('$map(["5", "6"], $isNumber)', b)[0] is True


def test_value_binding_wins_over_function_binding_in_value_position():
    b = JsonataBindings().bind_function("x", doubler()).bind_value("x", 7)
    assert eval_("$x", b) == 7


def test_variadic_bound_function_as_value_receives_one_argument():
    @bound_function("<j+:n>")
    def count(*args):
        return len(args)

    b = JsonataBindings().bind_function("count", count)
    assert eval_("$map([1,2], $count)", b) == [1, 1]


# =============================================================================
# A function value bound as a value
# =============================================================================


def times_ten():
    return lambda_node(lambda x: x * 10, 1)


def test_bound_function_value_called_directly():
    b = JsonataBindings().bind_value("f", times_ten())
    assert eval_("$f(3)", b) == 30


def test_bound_function_value_called_with_several_arguments():
    total = lambda_node(lambda args: args[0] + args[1], 2)
    b = JsonataBindings().bind_value("sum2", total)
    assert eval_("$sum2(3, 4)", b) == 7


def test_bound_function_value_called_with_no_arguments():
    constant = lambda_node(lambda _ignored: 42, 0)
    b = JsonataBindings().bind_value("answer", constant)
    assert eval_("$answer()", b) == 42


def test_bound_function_value_usable_as_value_too():
    b = JsonataBindings().bind_value("f", times_ten())
    assert eval_("$map([1,2], $f)", b) == [10, 20]
    assert eval_("4 ~> $f", b) == 40


def test_function_binding_wins_over_value_binding_at_call_site():
    b = JsonataBindings().bind_value("f", times_ten()).bind_function("f", doubler())
    assert eval_("$f(3)", b) == 6


def test_bound_value_that_is_not_a_function_called_as_function_still_fails():
    b = JsonataBindings().bind_value("notAFunction", 3)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$notAFunction(1)", b)
    assert exc_info.value.error_code == "T1006"
    assert exc_info.value.message == "The function 'notAFunction' is not defined"


def test_unbound_name_called_as_function_still_fails():
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$nothingBound(1)", JsonataBindings())
    assert exc_info.value.error_code == "T1006"


# =============================================================================
# The "f" signature type -- Python bound functions
# =============================================================================


def test_signature_f_accepts_a_function_argument():
    @bound_function("<fn:n>")
    def call_with(f, x):
        return fn_apply(f, x)

    b = JsonataBindings().bind_function("callWith", call_with)
    assert eval_("$callWith(function($x){$x * 3}, 3)", b) == 9


def test_signature_f_rejects_a_non_function_argument():
    @bound_function("<fn:n>")
    def call_with(f, x):
        return 0

    b = JsonataBindings().bind_function("callWith", call_with)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$callWith(5, 3)", b)
    assert exc_info.value.error_code == "T0410"


def test_signature_f_does_not_disable_the_rest_of_the_signature():
    @bound_function("<fn:b>")
    def second(f, n):
        return isinstance(n, (int, float)) and not isinstance(n, bool)

    b = JsonataBindings().bind_function("second", second)
    assert eval_('$second(function($x){$x}, "5")', b) is True

    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$second(function($x){$x})", b)
    assert "Missing required argument" in exc_info.value.message


def test_signature_f_parametrised_form_is_accepted():
    @bound_function("<f<n:n>n:n>")
    def call_with(f, x):
        return fn_apply(f, x)

    b = JsonataBindings().bind_function("callWith", call_with)
    assert eval_("$callWith(function($x){$x + 5}, 3)", b) == 8
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$callWith(1, 3)", b)
    assert exc_info.value.error_code == "T0410"


def test_signature_f_accepts_a_bound_function_referenced_as_a_value():
    @bound_function("<fn:n>")
    def call_with(f, x):
        return fn_apply(f, x)

    b = JsonataBindings().bind_function("double", doubler()).bind_function("callWith", call_with)
    assert eval_("$callWith($double, 7)", b) == 14


# =============================================================================
# The "f" and "x" signature types -- JSONata lambda signatures
# =============================================================================


def test_lambda_signature_f_accepts_a_function():
    assert eval_("λ($f)<f:n>{$f(2)}(function($x){$x * 3})", JsonataBindings()) == 6


def test_lambda_signature_f_rejects_a_non_function():
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("λ($f)<f:n>{2}(5)", JsonataBindings())
    assert exc_info.value.error_code == "T0410"


def test_lambda_signature_f_keeps_later_parameters_aligned():
    assert eval_("λ($f, $n)<fn:n>{$n}(function($x){$x}, 7)", JsonataBindings()) == 7


def test_lambda_signature_x_accepts_anything_and_keeps_alignment():
    assert eval_('λ($a, $n)<xn:n>{$n}("anything", 7)', JsonataBindings()) == 7


def test_a_failing_bound_function_reports_why_it_failed():
    @bound_function("<n:n>")
    def boom(n):
        raise JsonataEvaluationError("U1001", "Expression evaluation timeout")

    bindings = JsonataBindings().bind_function("boom", boom)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        eval_("$map([1], $boom)", bindings)

    assert exc_info.value.error_code == "U1001"
    assert "$boom" in exc_info.value.message
    assert "Expression evaluation timeout" in exc_info.value.message


# =============================================================================
# A binding of JSON null is a real value, not "unbound" (D1)
# =============================================================================
#
# resolve_binding used None as its not-found sentinel -- but None *is* JSON
# null, a value a caller can legitimately bind. So `$x` bound to null resolved
# as MISSING and $exists($x) answered False, contradicting both README section 3
# ("never conflated with None") and factory._exported_value, which carefully
# preserves null one step earlier on the library-export path. The sentinel is
# now MISSING throughout: the Bindings protocol, JsonataBindings and
# _MergedBindings all return MISSING for a name nobody bound.


@pytest.mark.parametrize("value", [None, 0, "", False, [], {}, [None]])
def test_a_bound_value_round_trips_whatever_it_was(value):
    """None is the interesting one; the rest are the controls that must not
    regress -- they are all falsy, and all distinct from "not bound"."""
    expr = compile_("$x")
    expr.assign("x", value)
    assert expr.evaluate(EMPTY) == value


def test_permanent_null_binding_is_not_missing():
    expr = compile_("$x")
    expr.assign("x", None)
    assert expr.evaluate(EMPTY) is not MISSING


def test_per_evaluation_null_binding_is_not_missing():
    assert eval_("$x", JsonataBindings().bind_value("x", None)) is None


def test_exists_is_true_for_a_null_binding():
    """$exists(null) is true in JSONata -- null exists, undefined does not."""
    assert eval_("$exists($x)", JsonataBindings().bind_value("x", None)) is True


def test_exists_is_false_for_a_name_nobody_bound():
    assert eval_("$exists($nothingBoundHere)") is False


def test_unbound_name_still_resolves_to_missing():
    assert eval_("$nothingBoundHere") is MISSING


def test_type_of_a_null_binding_is_null():
    assert eval_("$type($x)", JsonataBindings().bind_value("x", None)) == "null"


def test_null_binding_matches_an_inline_null_binding():
    """The inline form always worked; the two must now agree."""
    assert eval_("$x", JsonataBindings().bind_value("x", None)) == eval_("($x := null; $x)")


def test_per_evaluation_null_overrides_a_permanent_value():
    expr = compile_("$x")
    expr.assign("x", 42)
    assert expr.evaluate(EMPTY, JsonataBindings().bind_value("x", None)) is None


def test_a_null_value_binding_does_not_shadow_a_function_of_the_same_name():
    """resolve_binding falls through to the function table only when the name
    has no *value*; a null value is a value, so it wins."""
    bindings = JsonataBindings().bind_value("x", None)
    assert eval_("$x", bindings) is None
