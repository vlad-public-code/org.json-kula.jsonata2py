"""accept() dispatches through a type-keyed table rather than a chain of
class patterns, which is fast but silent: a node type with no table entry
is only discovered when something actually builds one.

These tests make the table's completeness a compile-time-ish property of
the test suite instead.
"""

from __future__ import annotations

import pytest

from jsonata2py.parser import ast_nodes
from jsonata2py.parser.ast_nodes import _VISIT_METHOD, AstNode, Visitor, accept


def _ast_node_classes() -> list[type]:
    """Every concrete AstNode subclass defined in ast_nodes.

    Reads module attributes rather than __subclasses__(): @dataclass with
    slots=True builds a replacement class and leaves the original
    registered as a subclass too, so __subclasses__() reports each node
    type twice.
    """
    out = []
    for name in dir(ast_nodes):
        obj = getattr(ast_nodes, name)
        if isinstance(obj, type) and issubclass(obj, AstNode) and obj is not AstNode:
            out.append(obj)
    return out


def test_every_ast_node_type_has_a_dispatch_entry():
    missing = [c.__name__ for c in _ast_node_classes() if c not in _VISIT_METHOD]
    assert not missing, f"AST node types with no accept() dispatch entry: {missing}"


def test_every_dispatch_target_exists_on_the_visitor_protocol():
    declared = {m for m in dir(Visitor) if m.startswith("visit_")}
    unknown = sorted(m for m in _VISIT_METHOD.values() if m not in declared)
    assert not unknown, f"dispatch table names methods the Visitor protocol does not declare: {unknown}"


def test_dispatch_table_has_no_stale_entries():
    stale = [c.__name__ for c in _VISIT_METHOD if getattr(ast_nodes, c.__name__, None) is not c]
    assert not stale, f"dispatch table holds class objects the module no longer exports: {stale}"


def test_unknown_node_type_is_reported_not_silently_ignored():
    class NotARealNode(AstNode):
        pass

    with pytest.raises(AssertionError, match="unhandled AST node: NotARealNode"):
        accept(NotARealNode(), object(), None)  # type: ignore[arg-type]
