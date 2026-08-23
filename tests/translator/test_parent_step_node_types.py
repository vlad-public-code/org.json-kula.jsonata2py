"""The parent operator `%` must be visible to parent tracking under every
node type that can contain an expression.

scope_analyzer._contains_parent_step_uncached decides whether a path needs
parent tracking. A node type missing from its walker does not fail loudly --
the `%` underneath it is simply invisible, so the translator never sets up
the parent frame and translation dies with S0217 ("cannot resolve parent")
for a perfectly valid expression. RangeExpr, ArraySubscript, ChainExpr,
TransformExpr, ElvisExpr, CoalesceExpr, PartialApplication and LambdaCall
were all missing; its sibling walker collect_free_vars_into already handled
them, which is what made the omission easy to miss.

Keep this file in step with that walker: one case per node type.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata

_FACTORY = jsonata.JsonataExpressionFactory()

_DATA = {"items": [{"n": 2, "vals": [9]}]}


@pytest.mark.parametrize(
    "expr,expected",
    [
        pytest.param("items.vals.[1..%.n]", [1, 2], id="RangeExpr"),
        pytest.param("items.vals.($[%.n - 2])", 9, id="ArraySubscript"),
        pytest.param("items.vals.(%.n ?? 5)", 2, id="CoalesceExpr"),
        pytest.param("items.vals.(%.n ?: 7)", 2, id="ElvisExpr"),
        pytest.param("items.vals.(%.n ~> $string())", "2", id="ChainExpr"),
        pytest.param("items.vals.($x := %.n; $x)", 2, id="Block-VariableBinding"),
        pytest.param("items.vals.%.n", 2, id="plain-parent-step"),
        pytest.param("items.vals.[%.n, %.n]", [2, 2], id="ArrayConstructor"),
        pytest.param('items.vals.{"k": %.n}', {"k": 2}, id="ObjectConstructor"),
    ],
)
def test_parent_step_is_seen_under_every_node_type(expr: str, expected: object) -> None:
    assert _FACTORY.compile(expr).evaluate(_DATA) == expected


def test_parent_step_inside_a_transform() -> None:
    data = {"items": [{"n": 2, "vals": [{"v": 1}]}]}
    result = _FACTORY.compile("items.vals.($ ~> |$|{'v': %.n}|)").evaluate(data)
    assert result == {"v": 2}
