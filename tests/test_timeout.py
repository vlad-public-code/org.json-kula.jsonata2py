"""Tests for expr.set_timeout (Phase 8 hardening gate).

Ported from TimeoutTest.java.
"""

from __future__ import annotations

import concurrent.futures

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataEvaluationError
from jsonata2py.runtime.values import MISSING

_FACTORY = jsonata.JsonataExpressionFactory()


def test_infinite_recursion_is_interrupted():
    expr = _FACTORY.compile("($loop := function($n) { $loop($n + 1) }; $loop(0))")
    expr.set_timeout(200)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        expr.evaluate(MISSING)
    assert exc_info.value.error_code == "U1001"


def test_large_range_is_interrupted():
    expr = _FACTORY.compile("$count([1..9999999])")
    expr.set_timeout(10)
    with pytest.raises(JsonataEvaluationError) as exc_info:
        expr.evaluate(MISSING)
    assert exc_info.value.error_code == "U1001"


def test_fast_expression_completes_normally():
    expr = _FACTORY.compile("1 + 2 + 3")
    expr.set_timeout(5000)
    assert expr.evaluate(MISSING) == 6


def test_no_timeout_by_default():
    expr = _FACTORY.compile("$sum([1..1000])")
    assert expr.evaluate(MISSING) == 500500


def test_disabling_timeout_allows_completion():
    expr = _FACTORY.compile("$sum([1..1000])")
    expr.set_timeout(200)
    expr.set_timeout(0)
    assert expr.evaluate(MISSING) == 500500


def test_timeout_is_per_evaluation_not_shared():
    expr = _FACTORY.compile("$sum([1..1000])")
    expr.set_timeout(5000)

    threads = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(expr.evaluate, MISSING) for _ in range(threads)]
        for f in futures:
            assert f.result() == 500500
