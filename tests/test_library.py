"""Tests for function libraries -- turning a JSONata definition expression
into JsonataBoundFunctions via JsonataExpressionFactory.compile_library
(Phase 7 gate).

Ported from JsonataLibraryTest.java. A definition expression is ordinary
JSONata: it binds named functions and returns the names of the ones to
export. The definitions below are the lambda examples from the
language-feature suite with their trailing invocation replaced by that
export list -- the invocation now happens from Python, or from a
different expression, which is what a library is for.
"""

from __future__ import annotations

import concurrent.futures
import math

import pytest

import jsonata2py as jsonata
from jsonata2py.bindings import JsonataBindings, JsonataBoundFunction, JsonataFunctionArguments, bound_function
from jsonata2py.errors import JsonataCompilationError, JsonataEvaluationError
from jsonata2py.library import JsonataLibrary, JsonataLibraryOptions

EMPTY: dict = {}

_FACTORY = jsonata.JsonataExpressionFactory()

# The trigonometry example from https://docs.jsonata.org/programming, exporting $sin and $cos.
TRIG = """
(
  $pi := 3.1415926535897932384626;

  /* Factorial is the product of the integers 1..n */
  $product := function($a, $b) { $a * $b };
  $factorial := function($n) { $n = 0 ? 1 : $reduce([1..$n], $product) };

  $sin := function($x){ /* define sine in terms of cosine */
    $cos($x - $pi/2)
  };
  $cos := function($x){ /* Derive cosine by expanding Maclaurin series */
    $x > $pi ? $cos($x - 2 * $pi) : $x < -$pi ? $cos($x + 2 * $pi) :
      $sum([0..12].($power(-1, $) * $power($x, 2*$) / $factorial(2*$)))
  };

  ["sin", "cos"]
)
"""


def call(fn: JsonataBoundFunction, *args: object) -> object:
    """Calls an exported function directly from Python, outside any evaluation."""
    return fn.apply(JsonataFunctionArguments(list(args)))


def export(definition: str, options: JsonataLibraryOptions | None = None) -> dict[str, JsonataBoundFunction]:
    return _FACTORY.compile_library(definition, options).functions


def library(definition: str, options: JsonataLibraryOptions | None = None) -> JsonataLibrary:
    return _FACTORY.compile_library(definition, options)


# =============================================================================
# The motivating example -- mutually recursive $sin / $cos
# =============================================================================


def test_trig_exported_functions_match_python_math():
    trig = export(TRIG)
    assert list(trig.keys()) == ["sin", "cos"]
    assert call(trig["sin"], 1) == pytest.approx(math.sin(1), abs=1e-12)
    assert call(trig["sin"], 0) == pytest.approx(math.sin(0), abs=1e-12)
    assert call(trig["cos"], 0) == pytest.approx(math.cos(0), abs=1e-12)
    assert call(trig["cos"], 2) == pytest.approx(math.cos(2), abs=1e-12)


def test_trig_definition_is_a_valid_jsonata_expression_on_its_own():
    result = _FACTORY.compile(TRIG).evaluate(None)
    assert result == ["sin", "cos"]


def test_trig_used_from_another_expression():
    trig = export(TRIG)
    expr = _FACTORY.compile("angles.$sin($)")
    for name, fn in trig.items():
        expr.register_function(name, fn)
    result = expr.evaluate({"angles": [0, 1, 2]})
    assert result == pytest.approx([math.sin(0), math.sin(1), math.sin(2)], abs=1e-12)


def test_trig_non_exported_helpers_stay_reachable():
    only = export(TRIG.replace('["sin", "cos"]', '["sin"]'))
    assert len(only) == 1
    assert call(only["sin"], 1) == pytest.approx(math.sin(1), abs=1e-12)


def test_trig_internal_helper_can_be_exported_too():
    fns = export(TRIG.replace('["sin", "cos"]', '["factorial", "product"]'))
    assert call(fns["factorial"], 5) == 120
    assert call(fns["product"], 3, 4) == 12


def test_trig_survives_repeated_use():
    trig = export(TRIG)
    expr = _FACTORY.compile("$sin(1)")
    for name, fn in trig.items():
        expr.register_function(name, fn)
    for _ in range(200):
        assert expr.evaluate(EMPTY) == pytest.approx(math.sin(1), abs=1e-12)


# =============================================================================
# Lambda shapes from the language-feature suite
# =============================================================================


def test_multi_param_lambda_volume():
    fns = export('($volume := function($l, $w, $h){ $l * $w * $h }; ["volume"])')
    assert call(fns["volume"], 10, 10, 5) == 500

    expr = _FACTORY.compile("$volume(2, 3, 4)")
    for name, fn in fns.items():
        expr.register_function(name, fn)
    assert expr.evaluate(EMPTY) == 24


def test_lambda_capturing_block_local_prefix():
    fns = export('($prefix := "Ph: "; $fmt := function($n){ $prefix & $n }; ["fmt"])')
    assert call(fns["fmt"], "0203 544 1234") == "Ph: 0203 544 1234"


def test_recursive_lambda_factorial():
    fns = export('($factorial := function($x){ $x <= 1 ? 1 : $x * $factorial($x-1) }; ["factorial"])')
    assert call(fns["factorial"], 4) == 24
    assert call(fns["factorial"], 10) == 3628800


def test_tail_recursive_lambda_factorial_with_accumulator():
    fns = export("""
        ($factorial := function($x){(
           $iter := function($x, $acc) {
             $x <= 1 ? $acc : $iter($x - 1, $x * $acc)
           };
           $iter($x, 1)
         )};
         ["factorial"])""")
    assert call(fns["factorial"], 10) == 3628800


def test_greek_lambda_fib():
    fns = export('($fib := λ($n) { $n <= 1 ? $n : $fib($n-1) + $fib($n-2) }; ["fib"])')
    expr = _FACTORY.compile("[1,2,3,4,5,6,7,8,9].$fib($)")
    for name, fn in fns.items():
        expr.register_function(name, fn)
    assert expr.evaluate(EMPTY) == [1, 1, 2, 3, 5, 8, 13, 21, 34]


def test_higher_order_lambda_returned_function_is_exportable():
    fns = export("""
        ($twice := function($f) { function($x){ $f($f($x)) } };
         $add3 := function($y){ $y + 3 };
         $add6 := $twice($add3);
         ["add6", "add3"])""")
    assert call(fns["add6"], 7) == 13
    assert call(fns["add3"], 7) == 10


def test_function_chaining_normalize_whitespace():
    fns = export('($normalize := $uppercase ~> $trim; ["normalize"])')
    assert call(fns["normalize"], "   Some   Words   ") == "SOME WORDS"


def test_partial_application_first5():
    fns = export('($first5 := $substring(?, 0, 5); ["first5"])')
    assert call(fns["first5"], "Hello, World") == "Hello"


def test_higher_order_builtins_inside_exported_body():
    fns = export("""
        (
          $doubleAll := function($a){ $map($a, function($v){ $v * 2 }) };
          $bigOnes := function($a, $min){ $filter($a, function($v){ $v > $min }) };
          $total := function($a){ $reduce($a, function($acc, $v){ $acc + $v }) };

          ["doubleAll", "bigOnes", "total"]
        )""")
    expr = _FACTORY.compile("$total($bigOnes($doubleAll([1,2,3,4,5]), 4))")
    for name, fn in fns.items():
        expr.register_function(name, fn)
    assert expr.evaluate(EMPTY) == 24


# =============================================================================
# The export list
# =============================================================================


def test_export_list_accepts_dollar_prefixed_names():
    fns = export(TRIG.replace('["sin", "cos"]', '["$sin", "$cos"]'))
    assert list(fns.keys()) == ["sin", "cos"]
    assert call(fns["sin"], 1) == pytest.approx(math.sin(1), abs=1e-12)


def test_export_list_accepts_a_single_unwrapped_name():
    fns = export('($double := function($x){ $x * 2 }; "double")')
    assert list(fns.keys()) == ["double"]
    assert call(fns["double"], 4) == 8


def test_export_list_may_be_computed():
    fns = export("""
        (
          $inc := function($x){ $x + 1 };
          $dec := function($x){ $x - 1 };
          $exports := ["inc", "dec"];
          $withDec := true;

          $withDec ? $exports : $exports[0]
        )""")
    assert list(fns.keys()) == ["inc", "dec"]
    assert call(fns["inc"], 4) == 5
    assert call(fns["dec"], 4) == 3


def test_export_list_order_is_preserved():
    fns = export(TRIG.replace('["sin", "cos"]', '["cos", "sin", "factorial"]'))
    assert list(fns.keys()) == ["cos", "sin", "factorial"]


def test_definition_without_parentheses_single_binding():
    fns = export('($double := function($x){ $x * 2 }; ["double"])')
    assert call(fns["double"], 4) == 8


def test_definition_may_bind_an_export_names_variable_itself():
    fns = export("""
        (
          $__exportNames := "not the export list";
          $echo := function($x){ $x & "/" & $__exportNames };
          ["echo"]
        )""")
    assert call(fns["echo"], "a") == "a/not the export list"


# =============================================================================
# Constants -- exported names that are not functions
# =============================================================================


def test_constants_are_sorted_from_functions_by_what_they_evaluate_to():
    lib = library(TRIG.replace('["sin", "cos"]', '["sin", "cos", "pi"]'))
    assert list(lib.functions.keys()) == ["sin", "cos"]
    assert list(lib.constants.keys()) == ["pi"]
    assert lib.constants["pi"] == pytest.approx(math.pi, abs=1e-12)


def test_constants_are_empty_when_only_functions_are_exported():
    lib = library(TRIG)
    assert lib.constants == {}
    assert len(lib.functions) == 2


def test_constants_of_every_json_type():
    lib = library("""
        (
          $count := 42;
          $name := "acme";
          $active := true;
          $nothing := null;
          $rates := {"vat": 0.2, "duty": 0.1};
          $regions := ["eu", "us"];

          ["count", "name", "active", "nothing", "rates", "regions"]
        )""")
    constants = lib.constants
    assert lib.functions == {}
    assert constants["count"] == 42
    assert constants["name"] == "acme"
    assert constants["active"] is True
    assert constants["nothing"] is None
    assert constants["rates"]["vat"] == pytest.approx(0.2, abs=1e-12)
    assert constants["regions"] == ["eu", "us"]


def test_constants_are_computed_once_at_compile_time():
    lib = library('($total := $sum([1..10]); ["total"])')
    assert lib.constants["total"] == 55


def test_constants_drop_straight_into_an_expression():
    lib = library("""
        (
          $pi := 3.14159;
          $area := function($r){ $pi * $r * $r };
          ["area", "pi"]
        )""")
    expr = _FACTORY.compile('{"area": $area(2), "pi": $pi}')
    for name, fn in lib.functions.items():
        expr.register_function(name, fn)
    for name, value in lib.constants.items():
        expr.assign(name, value)

    result = expr.evaluate(EMPTY)
    assert result["area"] == pytest.approx(12.56636, abs=1e-9)
    assert result["pi"] == pytest.approx(3.14159, abs=1e-9)


def test_constants_bind_per_evaluation_too():
    lib = library('($vat := 0.2; ["vat"])')
    expr = _FACTORY.compile("100 * (1 + $vat)")
    bindings = JsonataBindings().use_library(lib)
    assert expr.evaluate(EMPTY, bindings) == pytest.approx(120.0, abs=1e-9)


# =============================================================================
# use_library -- applying a library's whole export set at once
# =============================================================================


def test_use_library_binds_functions_and_constants_together():
    lib = library("""
        (
          $pi := 3.14159;
          $area := function($r){ $pi * $r * $r };
          ["area", "pi"]
        )""")
    expr = _FACTORY.compile('{"area": $area(2), "pi": $pi}')
    result = expr.evaluate(EMPTY, JsonataBindings().use_library(lib))
    assert result["area"] == pytest.approx(12.56636, abs=1e-9)
    assert result["pi"] == pytest.approx(3.14159, abs=1e-9)


def test_use_library_chains_with_other_bindings():
    lib = library('($vat := 0.2; ["vat"])')
    bindings = JsonataBindings().use_library(lib).bind_value("net", 100)
    assert _FACTORY.compile("$net * (1 + $vat)").evaluate(EMPTY, bindings) == pytest.approx(120.0, abs=1e-9)


def test_use_library_applies_several_libraries_last_name_wins():
    first = library('($tag := function(){ "first" }; $n := 1; ["tag", "n"])')
    second = library('($tag := function(){ "second" }; ["tag"])')
    bindings = JsonataBindings().use_library(first).use_library(second)
    result = _FACTORY.compile('{"tag": $tag(), "n": $n}').evaluate(EMPTY, bindings)
    assert result["tag"] == "second"
    assert result["n"] == 1


def test_use_library_exports_are_usable_as_values_too():
    lib = library('($inc := function($x){ $x + 1 }; ["inc"])')
    result = _FACTORY.compile("$map([1,2,3], $inc)").evaluate(EMPTY, JsonataBindings().use_library(lib))
    assert result == [2, 3, 4]


def test_use_library_of_a_closed_library_binds_but_refuses_to_run():
    lib = library('($vat := 0.2; $gross := function($n){ $n * (1 + $vat) }; ["gross", "vat"])')
    bindings = JsonataBindings().use_library(lib)
    lib.close()

    assert _FACTORY.compile("$vat").evaluate(EMPTY, bindings) == pytest.approx(0.2, abs=1e-9)
    with pytest.raises(JsonataEvaluationError):
        _FACTORY.compile("$gross(100)").evaluate(EMPTY, bindings)


# =============================================================================
# use_library on the expression -- the same thing, permanently
# =============================================================================


def test_use_library_on_expression_binds_functions_and_constants_together():
    lib = library("""
        (
          $pi := 3.14159;
          $area := function($r){ $pi * $r * $r };
          ["area", "pi"]
        )""")
    expr = _FACTORY.compile('{"area": $area(2), "pi": $pi}')
    expr.use_library(lib)

    for _ in range(2):
        result = expr.evaluate(EMPTY)
        assert result["area"] == pytest.approx(12.56636, abs=1e-9)
        assert result["pi"] == pytest.approx(3.14159, abs=1e-9)


def test_use_library_on_expression_applies_several_libraries_last_name_wins():
    first = library('($tag := function(){ "first" }; $n := 1; ["tag", "n"])')
    second = library('($tag := function(){ "second" }; ["tag"])')
    expr = _FACTORY.compile('{"tag": $tag(), "n": $n}')
    expr.use_library(first)
    expr.use_library(second)
    result = expr.evaluate(EMPTY)
    assert result["tag"] == "second"
    assert result["n"] == 1


def test_use_library_on_expression_per_evaluation_binding_wins():
    lib = library('($vat := 0.2; $gross := function($n){ $n * 1.2 }; ["gross", "vat"])')
    expr = _FACTORY.compile('{"vat": $vat, "gross": $gross(100)}')
    expr.use_library(lib)

    @bound_function("<n:n>")
    def gross(n):
        return 1

    overridden = expr.evaluate(EMPTY, JsonataBindings().bind_value("vat", 0.05).bind_function("gross", gross))
    assert overridden["vat"] == pytest.approx(0.05, abs=1e-9)
    assert overridden["gross"] == 1

    plain = expr.evaluate(EMPTY)
    assert plain["vat"] == pytest.approx(0.2, abs=1e-9)
    assert plain["gross"] == pytest.approx(120.0, abs=1e-9)


def test_use_library_on_expression_of_a_closed_library_binds_but_refuses_to_run():
    lib = library('($vat := 0.2; $gross := function($n){ $n * (1 + $vat) }; ["gross", "vat"])')
    constant = _FACTORY.compile("$vat")
    call_expr = _FACTORY.compile("$gross(100)")
    constant.use_library(lib)
    call_expr.use_library(lib)
    lib.close()

    assert constant.evaluate(EMPTY) == pytest.approx(0.2, abs=1e-9)
    with pytest.raises(JsonataEvaluationError):
        call_expr.evaluate(EMPTY)


def test_constants_from_a_definition_that_mixes_both_and_depends_on_its_own_values():
    lib = library("""
        (
          $rates := {"standard": 0.2, "reduced": 0.05};
          $gross := function($net, $band){ $net * (1 + $lookup($rates, $band)) };
          ["gross", "rates"]
        )""")
    assert call(lib.functions["gross"], 100, "standard") == pytest.approx(120.0, abs=1e-9)
    assert lib.constants["rates"]["reduced"] == pytest.approx(0.05, abs=1e-12)


def test_constants_survives_close():
    lib = library('($pi := 3.14159; $f := function($x){ $x }; ["pi", "f"])')
    pi = lib.constants["pi"]
    f = lib.functions["f"]
    lib.close()

    assert pi == pytest.approx(3.14159, abs=1e-9)
    assert lib.constants["pi"] == pytest.approx(3.14159, abs=1e-9)
    with pytest.raises(JsonataEvaluationError):
        call(f, 1)


# =============================================================================
# Signatures and argument handling
# =============================================================================


def test_declared_signature_is_reported_and_coerces():
    fns = export('($twice := function($x)<n:n>{ $x * 2 }; ["twice"])')
    assert fns["twice"].get_function_signature() == "<n:n>"

    expr = _FACTORY.compile('$twice("21")')
    for name, fn in fns.items():
        expr.register_function(name, fn)
    assert expr.evaluate(EMPTY) == 42


def test_synthesized_signature_is_all_optional():
    fns = export('($volume := function($l, $w, $h){ $l * $w * $h }; ["volume"])')
    assert fns["volume"].get_function_signature() == "<j?j?j?:j>"


def test_signature_override_applies_coercion():
    lib = _FACTORY.compile_library(
        '($twice := function($x){ $x * 2 }; ["twice"])', JsonataLibraryOptions().with_signature("$twice", "<n:n>")
    )
    assert lib.functions["twice"].get_function_signature() == "<n:n>"

    expr = _FACTORY.compile('$twice("21")')
    for name, fn in lib.functions.items():
        expr.register_function(name, fn)
    assert expr.evaluate(EMPTY) == 42


def test_missing_argument_becomes_undefined_not_an_error():
    fns = export(
        '($greet := function($name, $greeting){ ($greeting ? $greeting : "Hello") & ", " & $name }; ["greet"])'
    )
    expr = _FACTORY.compile('$greet("Fred")')
    for name, fn in fns.items():
        expr.register_function(name, fn)
    assert expr.evaluate(EMPTY) == "Hello, Fred"


def test_array_argument_is_not_flattened():
    fns = export('($count2 := function($a){ $count($a) }; ["count2"])')
    assert call(fns["count2"], [1, 2, 3]) == 3


# =============================================================================
# Bindings integration
# =============================================================================


def test_exported_functions_bind_per_evaluation():
    trig = export(TRIG)
    expr = _FACTORY.compile("$sin($angle)")
    bindings = JsonataBindings().bind_value("angle", 1).bind_functions(trig)
    assert expr.evaluate(EMPTY, bindings) == pytest.approx(math.sin(1), abs=1e-12)


def test_free_variable_resolves_against_definition_bindings_when_called_from_python():
    lib = _FACTORY.compile_library(
        '($withVat := function($net){ $net * (1 + $vatRate) }; ["withVat"])',
        JsonataLibraryOptions().with_bindings(JsonataBindings().bind_value("vatRate", 0.2)),
    )
    assert call(lib.functions["withVat"], 100) == pytest.approx(120.0, abs=1e-9)


# =============================================================================
# A definition must be self-contained
# =============================================================================


def test_error_definition_uses_an_unbound_variable():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($withVat := function($net){ $net * (1 + $vatRate) }; ["withVat"])')
    msg = exc_info.value.message
    assert "$vatRate" in msg
    assert "does not bind" in msg
    assert "JsonataLibraryOptions.bindings" in msg


def test_error_definition_calls_an_unbound_function():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($f := function($x){ $helper($x) }; ["f"])')
    assert "$helper" in exc_info.value.message


def test_error_unbound_names_are_all_reported():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($f := function($x){ $x * $rate + $offset }; ["f"])')
    msg = exc_info.value.message
    assert "$rate" in msg
    assert "$offset" in msg
    assert "are not JSONata built-ins" in msg


def test_error_typo_in_an_exported_name_is_caught_as_an_unbound_reference():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($rate := 0.2; $f := function($x){ $x * $rat }; ["f"])')
    assert "$rat" in exc_info.value.message


def test_self_contained_builtins_are_not_free_variables():
    fns = export("""
        (
          $stats := function($a){ {"n": $count($a), "total": $sum($a), "avg": $average($a)} };
          $shout := $uppercase ~> $trim;
          $head := $substring(?, 0, 3);
          ["stats", "shout", "head"]
        )""")
    assert len(fns) == 3
    assert call(fns["stats"], [1, 2, 3])["total"] == 6


def test_self_contained_lambda_parameters_and_inner_bindings_are_bound():
    fns = export('($f := function($x, $y){ ( $sum := $x + $y; $scale := 2; $sum * $scale ) }; ["f"])')
    assert call(fns["f"], 3, 4) == 14


def test_self_contained_forward_references_between_siblings_are_bound():
    assert len(export(TRIG)) == 2


def test_self_contained_path_bindings_are_bound():
    fns = export('($labels := function($a){ $a@$v#$i.($string($i) & ":" & $v) }; ["labels"])')
    assert call(fns["labels"], ["a", "b"]) == ["0:a", "1:b"]


def test_self_contained_names_supplied_through_options_are_accepted():
    lib = _FACTORY.compile_library(
        '($withVat := function($net){ $net * (1 + $vatRate) }; ["withVat"])',
        JsonataLibraryOptions().with_bindings(JsonataBindings().bind_value("vatRate", 0.2)),
    )
    assert call(lib.functions["withVat"], 100) == pytest.approx(120.0, abs=1e-9)


def test_self_contained_functions_supplied_through_options_are_accepted():
    @bound_function("<n:n>")
    def triple(n):
        return n * 3

    provided = JsonataBindings().bind_function("triple", triple)
    lib = _FACTORY.compile_library('($f := function($x){ $triple($x) + 1 }; ["f"])', JsonataLibraryOptions().with_bindings(provided))
    assert call(lib.functions["f"], 4) == 13


def test_definition_input_is_available_to_the_definition():
    lib = _FACTORY.compile_library(
        '($factor := rates.vat; $rate := function($net){ $net * $factor }; ["rate"])',
        JsonataLibraryOptions().with_input({"rates": {"vat": 1.2}}),
    )
    assert call(lib.functions["rate"], 100) == pytest.approx(120.0, abs=1e-9)


# =============================================================================
# Map shape and lifetime
# =============================================================================


def test_library_keys_never_carry_the_dollar():
    lib = library(TRIG)
    assert lib.functions.get("sin") is not None
    assert lib.functions.get("$sin") is None
    assert lib.functions.get("nope") is None


def test_library_maps_are_defensive_copies():
    lib = library(TRIG)
    functions = lib.functions
    functions.pop("sin")
    constants = lib.constants
    constants["extra"] = 1
    assert "sin" in lib.functions
    assert "extra" not in lib.constants


def test_library_close_releases_the_functions():
    lib = _FACTORY.compile_library(TRIG)
    sin = lib.functions["sin"]
    assert lib.is_open
    assert call(sin, 1) == pytest.approx(math.sin(1), abs=1e-12)

    lib.close()

    assert not lib.is_open
    with pytest.raises(JsonataEvaluationError) as exc_info:
        call(sin, 1)
    assert "$sin" in exc_info.value.message


def test_library_close_is_idempotent():
    lib = _FACTORY.compile_library(TRIG)
    lib.close()
    lib.close()  # must not raise


def test_library_reports_its_source():
    lib = _FACTORY.compile_library(TRIG)
    assert lib.source_jsonata == TRIG


def test_two_libraries_are_independent():
    a = export('($f := function($x){ $x + 1 }; ["f"])')
    b = _FACTORY.compile_library('($f := function($x){ $x + 100 }; ["f"])')

    assert call(a["f"], 1) == 2
    assert call(b.functions["f"], 1) == 101

    b.close()
    assert call(a["f"], 1) == 2


# =============================================================================
# Errors
# =============================================================================


def test_error_definition_does_not_return_an_export_list():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export("($sin := function($x){ $x };)")
    msg = exc_info.value.message
    assert "array of function names" in msg
    assert "function" in msg


def test_error_definition_returns_nothing():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($f := function($x){ $x }; nothing.here)')
    assert "returned nothing" in exc_info.value.message


def test_error_definition_returns_non_strings():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($f := function($x){ $x }; [1, 2])')
    assert "array of function names" in exc_info.value.message


def test_error_definition_returns_an_empty_list():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export('($f := function($x){ $x }; [])')
    msg = exc_info.value.message
    assert "nothing" in msg or "empty" in msg


def test_error_name_not_defined_at_top_level():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export(TRIG.replace('["sin", "cos"]', '["sin", "tan"]'))
    msg = exc_info.value.message
    assert "$tan" in msg
    assert "not defined" in msg


def test_error_name_bound_only_inside_a_nested_block():
    definition = """
        ($outer := function($x){(
           $inner := function($y){ $y * 2 };
           $inner($x)
         )};
         ["inner"])"""
    with pytest.raises(JsonataCompilationError) as exc_info:
        export(definition)
    assert "$inner" in exc_info.value.message


def test_error_duplicate_names_in_the_export_list():
    with pytest.raises(JsonataCompilationError) as exc_info:
        export(TRIG.replace('["sin", "cos"]', '["sin", "$sin"]'))
    assert "twice" in exc_info.value.message


def test_error_invalid_definition_expression():
    with pytest.raises(JsonataCompilationError):
        export('($f := function($x){ $x + }; ["f"])')


def test_error_definition_that_throws():
    with pytest.raises(JsonataCompilationError):
        export('($f := $error("nope"); $g := function($x){ $x }; ["g"])')


def test_error_calling_an_exported_function_that_fails():
    fns = export('($boom := function($x){ $error("boom " & $x) }; ["boom"])')
    with pytest.raises(JsonataEvaluationError) as exc_info:
        call(fns["boom"], 1)
    assert "$boom" in exc_info.value.message


# =============================================================================
# Concurrency
# =============================================================================


def test_exported_functions_are_thread_safe():
    trig = export(TRIG)
    expr = _FACTORY.compile("$sin($angle)")
    for name, fn in trig.items():
        expr.register_function(name, fn)

    threads = 8
    iterations = 50

    def worker(angle: float) -> int:
        successes = 0
        for _ in range(iterations):
            b = JsonataBindings().bind_value("angle", angle)
            result = expr.evaluate(EMPTY, b)
            assert abs(result - math.sin(angle)) <= 1e-12
            successes += 1
        return successes

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, t / 4.0) for t in range(threads)]
        total = sum(f.result() for f in futures)
    assert total == threads * iterations


# =============================================================================
# Regression -- ordinary expressions are unaffected
# =============================================================================


def test_ordinary_lambdas_still_evaluate_normally():
    assert _FACTORY.compile(
        "($volume := function($l, $w, $h){ $l * $w * $h }; $volume(10, 10, 5))"
    ).evaluate(EMPTY) == 500
    assert _FACTORY.compile(
        "($factorial := function($x){ $x <= 1 ? 1 : $x * $factorial($x-1) }; $factorial(4))"
    ).evaluate(EMPTY) == 24
    assert _FACTORY.compile(TRIG.replace('["sin", "cos"]', "$sin(1)")).evaluate(None) == pytest.approx(
        math.sin(1), abs=1e-12
    )


def test_function_values_are_not_strings():
    expr = _FACTORY.compile('($f := function($x){ $x }; {"f": $f})')
    result = expr.evaluate(EMPTY)
    fn = result["f"]
    assert not isinstance(fn, str)
    assert _FACTORY.compile("$type(f)").evaluate(result) == "function"


def test_function_values_outlive_the_evaluation_that_created_them():
    fns = export('($add := function($a,$b){ $a + $b }; ["add"])')
    for _ in range(5):
        _FACTORY.compile("1 + 1").evaluate(EMPTY)
    assert call(fns["add"], 3, 4) == 7
