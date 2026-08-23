"""Head-to-head performance comparison between jsonata2py and
two other Python JSONata implementations:

  * jsonata-python (PyPI "jsonata-python") -- the reference AST-interpreter
    implementation, pure Python.
  * jsonatapy (PyPI "jsonatapy") -- a Rust-backed (PyO3) implementation.
    This one is not a fair "pure Python vs pure Python" comparison: it
    runs compiled native code, the same category of advantage the Java
    project has over an AST interpreter, just via a different route
    (native code instead of JIT bytecode). Included anyway because the
    user asked for it, but read its numbers as "how does this compare to
    a natively-compiled alternative," not as validation of the "2-5x over
    a pure-Python interpreter" target below.

Ported from PerformanceComparisonTest.java (§11 of the design spec).

The design spec is explicit that the Java project's ~40x-over-JSONata4Java
headline does NOT carry over: CPython has no JIT, so generated Python
source still runs on the same interpreter as an AST walker would. The
realistic target here is 2-5x over jsonata-python, with compilation
costing under 5ms. This file measures that -- it does not assert it,
because a cross-implementation comparison depends on which versions of
the other two libraries happen to be installed. Run it explicitly with
`pytest tests/benchmarks -m benchmark`.

Regressions in *this* library are gated separately, against a per-machine
baseline: see test_performance_regression.py.

The expression (benchmark_expression.jsonata) and input document
(benchmark_input.json) are copied verbatim from the Java project's
src/test/resources/benchmark/ so all implementations are measured on
exactly the same workload the JVM comparison uses.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

import jsonata2py as jsonata

# jsonata-python and jsonata-rs BOTH install a top-level module named
# `jsonata`, so they overwrite each other and can never be present in the same
# environment. Whichever one is installed owns the name; we identify it by a
# distinguishing export rather than by import name, and skip the other.
# To measure both, use two virtualenvs.
jsonata_python: Any = None
jsonata_rs: Any = None
_HAS_JSONATA_PYTHON = False
_HAS_JSONATA_RS = False
try:
    import jsonata as _jsonata_module

    if hasattr(_jsonata_module, "JsonataInternalError"):
        jsonata_rs = _jsonata_module  # PyPI "jsonata-rs" (Rust/PyO3)
        _HAS_JSONATA_RS = True
    elif hasattr(_jsonata_module, "JException"):
        jsonata_python = _jsonata_module  # PyPI "jsonata-python" (pure-Python interpreter)
        _HAS_JSONATA_PYTHON = True
except ImportError:
    pass

try:
    import jsonatapy  # Rust-backed (PyO3), PyPI package "jsonatapy"

    _HAS_JSONATAPY = True
except ImportError:
    _HAS_JSONATAPY = False

pytestmark = pytest.mark.benchmark

_RESOURCES = Path(__file__).parent.parent / "resources" / "benchmark"
_EXPRESSION_SOURCE = (_RESOURCES / "benchmark_expression.jsonata").read_text(encoding="utf-8")
_INPUT_DATA: Any = json.loads((_RESOURCES / "benchmark_input.json").read_text(encoding="utf-8"))

_EXPECTED = {
    "company": "Acme Corporation",
    "founded": 1985,
    "totalEmployees": 19,
    "departments": 4,
    "activeProducts": 6,
    "delivered": 3,
    "skuCount": 7,
}


def _check_result(result: dict) -> None:
    assert result["company"] == _EXPECTED["company"]
    assert result["founded"] == _EXPECTED["founded"]
    assert result["workforce"]["totalEmployees"] == _EXPECTED["totalEmployees"]
    assert result["workforce"]["departments"] == _EXPECTED["departments"]
    assert result["catalog"]["active"] == _EXPECTED["activeProducts"]
    assert result["orders"]["delivered"] == _EXPECTED["delivered"]
    assert result["orders"]["skuCount"] == _EXPECTED["skuCount"]
    assert result["summary"].startswith("Company Acme Corporation")


# =============================================================================
# Correctness -- both implementations must agree before their speed means anything
# =============================================================================


def test_this_compiler_produces_correct_results():
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile(_EXPRESSION_SOURCE)
    _check_result(expr.evaluate(_INPUT_DATA))


@pytest.mark.skipif(not _HAS_JSONATA_PYTHON, reason="jsonata-python not installed")
def test_jsonata_python_produces_correct_results():
    expr = jsonata_python.Jsonata(_EXPRESSION_SOURCE)
    _check_result(expr.evaluate(_INPUT_DATA))


@pytest.mark.skipif(not _HAS_JSONATAPY, reason="jsonatapy not installed")
def test_jsonatapy_produces_correct_results():
    expr = jsonatapy.compile(_EXPRESSION_SOURCE)
    _check_result(expr.evaluate(_INPUT_DATA))


@pytest.mark.skipif(not _HAS_JSONATA_RS, reason="jsonata-rs not installed (or jsonata-python owns `jsonata`)")
def test_jsonata_rs_produces_correct_results():
    expr = jsonata_rs.Jsonata(_EXPRESSION_SOURCE)
    _check_result(expr.evaluate(_INPUT_DATA))


# =============================================================================
# Compilation cost
# =============================================================================

# jsonata2py caches compiled artefacts keyed on expression text
# (factory.py's weakref-based _compile_cache) -- a repeat compile() of text
# an earlier CompiledExpression is still holding onto is nearly free. If this
# benchmark called factory.compile() on the *same* text every round the way
# the other two libraries' benchmarks do, pytest-benchmark's repeated calls
# would hit that cache after round 1 and report the cache-hit cost, not a
# genuine compile -- not an apples-to-apples number against two libraries
# that don't cache at all. So each round gets a fresh factory and uniquely
# suffixed text (a JSONata block comment, semantically inert) to force a
# real cold compile every time. The cache-hit number is measured separately
# below, as its own thing -- there is no equivalent to compare it against.
_cold_compile_counter = itertools.count()


def _cold_compile_round():
    factory = jsonata.JsonataExpressionFactory()
    text = _EXPRESSION_SOURCE + f"/* bench-{next(_cold_compile_counter)} */"
    return (factory, text), {}


def test_this_compiler_compilation_cost(benchmark):
    benchmark.pedantic(lambda factory, text: factory.compile(text), setup=_cold_compile_round, rounds=50)


def test_this_compiler_repeat_compile_cost(benchmark):
    """Not a head-to-head row -- neither jsonata-python nor jsonatapy caches
    compiled expressions, so there's nothing to compare this against.
    Documents the real, separate number for the case the cache targets:
    repeat-compiling text an earlier CompiledExpression from the same
    factory is still holding alive (see factory.py's _compile_cache)."""
    factory = jsonata.JsonataExpressionFactory()
    keep_alive = factory.compile(_EXPRESSION_SOURCE)  # keeps entry_point reachable -> guaranteed cache hits
    benchmark(factory.compile, _EXPRESSION_SOURCE)
    assert keep_alive is not None


@pytest.mark.skipif(not _HAS_JSONATA_PYTHON, reason="jsonata-python not installed")
def test_jsonata_python_compilation_cost(benchmark):
    benchmark(jsonata_python.Jsonata, _EXPRESSION_SOURCE)


@pytest.mark.skipif(not _HAS_JSONATAPY, reason="jsonatapy not installed")
def test_jsonatapy_compilation_cost(benchmark):
    benchmark(jsonatapy.compile, _EXPRESSION_SOURCE)


@pytest.mark.skipif(not _HAS_JSONATA_RS, reason="jsonata-rs not installed (or jsonata-python owns `jsonata`)")
def test_jsonata_rs_compilation_cost(benchmark):
    benchmark(jsonata_rs.Jsonata, _EXPRESSION_SOURCE)


# =============================================================================
# Evaluation throughput -- compile once, evaluate repeatedly (pytest-benchmark
# handles its own warmup/round calibration; this is the "compile once,
# evaluate many" shape the library is designed around)
# =============================================================================


def test_this_compiler_evaluation_throughput(benchmark):
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile(_EXPRESSION_SOURCE)
    result = benchmark(expr.evaluate, _INPUT_DATA)
    _check_result(result)


@pytest.mark.skipif(not _HAS_JSONATA_PYTHON, reason="jsonata-python not installed")
def test_jsonata_python_evaluation_throughput(benchmark):
    expr = jsonata_python.Jsonata(_EXPRESSION_SOURCE)
    result = benchmark(expr.evaluate, _INPUT_DATA)
    _check_result(result)


@pytest.mark.skipif(not _HAS_JSONATAPY, reason="jsonatapy not installed")
def test_jsonatapy_evaluation_throughput(benchmark):
    expr = jsonatapy.compile(_EXPRESSION_SOURCE)
    result = benchmark(expr.evaluate, _INPUT_DATA)
    _check_result(result)


@pytest.mark.skipif(not _HAS_JSONATA_RS, reason="jsonata-rs not installed (or jsonata-python owns `jsonata`)")
def test_jsonata_rs_evaluation_throughput(benchmark):
    expr = jsonata_rs.Jsonata(_EXPRESSION_SOURCE)
    result = benchmark(expr.evaluate, _INPUT_DATA)
    _check_result(result)
