"""evaluate() must never modify the document it is handed.

README's thread-safety section promises evaluation "returns a new value
without modifying any shared state", and callers rely on it: a document is
routinely evaluated by several expressions, or by the same expression twice.

The regression pinned here is the one the official-suite harness
(test_suite_end_to_end.py) now asserts for every case: merge_group_by_objects
merged duplicate group-by keys by extend()-ing the list it had already stored
-- and that list is frequently navigated straight out of the input document,
because field() on a dict returns the aliased original rather than a copy.
The first evaluate() therefore returned the right answer while quietly
corrupting the caller's data; only a *second* evaluate() on the same document
revealed it. Hence the two assertions in each test below: the input is
unchanged, and the expression is repeatable.
"""

from __future__ import annotations

import copy

import pytest

import jsonata2py as jsonata

_FACTORY = jsonata.JsonataExpressionFactory()

# expression, input document, expected result
_GROUP_BY_CASES = [
    pytest.param(
        'orders@$o.prod{"k": $o.lst}',
        {"orders": [{"lst": [1, 2]}, {"lst": [3, 4]}], "prod": [{"p": 1}]},
        {"k": [1, 2, 3, 4]},
        id="list-values-collide",
    ),
    pytest.param(
        'orders@$o.prod{"k": $o.n}',
        {"orders": [{"n": 1}, {"n": 2}], "prod": [{"p": 1}]},
        {"k": [1, 2]},
        id="scalar-values-collide",
    ),
    pytest.param(
        'orders@$o.prod{"k": $o.mix}',
        {"orders": [{"mix": 1}, {"mix": [2, 3]}], "prod": [{"p": 1}]},
        {"k": [1, 2, 3]},
        id="scalar-then-list",
    ),
    pytest.param(
        'orders@$o.prod{"k": $o.lst}',
        {"orders": [{"lst": [1, 2]}, {"lst": [3, 4]}, {"lst": [5]}], "prod": [{"p": 1}]},
        {"k": [1, 2, 3, 4, 5]},
        id="three-way-collision",
    ),
]


@pytest.mark.parametrize("expr,data,expected", _GROUP_BY_CASES)
def test_group_by_merge_does_not_mutate_input(expr: str, data: dict, expected: dict) -> None:
    before = copy.deepcopy(data)
    assert _FACTORY.compile(expr).evaluate(data) == expected
    assert data == before, "evaluation modified the input document"


@pytest.mark.parametrize("expr,data,expected", _GROUP_BY_CASES)
def test_group_by_merge_is_repeatable(expr: str, data: dict, expected: dict) -> None:
    """Re-evaluating the same document must give the same answer. An
    in-place merge shows up here even when the first result looks right."""
    compiled = _FACTORY.compile(expr)
    assert compiled.evaluate(data) == expected
    assert compiled.evaluate(data) == expected
    assert compiled.evaluate(data) == expected


def test_independent_expressions_see_an_uncorrupted_document() -> None:
    """A shared document evaluated by one expression must still read
    correctly through another."""
    data = {"orders": [{"lst": [1, 2]}, {"lst": [3, 4]}], "prod": [{"p": 1}]}
    _FACTORY.compile('orders@$o.prod{"k": $o.lst}').evaluate(data)
    assert _FACTORY.compile("orders.lst").evaluate(data) == [1, 2, 3, 4]
    assert _FACTORY.compile("orders[0].lst").evaluate(data) == [1, 2]
