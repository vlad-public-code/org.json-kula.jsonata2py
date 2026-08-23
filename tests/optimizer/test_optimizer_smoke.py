"""Phase 2 gate (partial): the optimizer must run without crashing on every
expression in the official JSONata test suite that parses successfully.

Structural equivalence against the Java optimizer's output on a fixture
corpus (the other half of the Phase 2 gate) is deferred until the
translator exists and generated-code-level testing can cross-check
optimized vs. unoptimized ASTs by evaluation result instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jsonata2py.errors import ParseError
from jsonata2py.optimizer.optimizer import optimize
from jsonata2py.parser.parser import Parser

SUITE_DIR = Path(__file__).parent.parent / "resources" / "jsonata-test-suite" / "groups"


def _iter_cases():
    for path in sorted(SUITE_DIR.rglob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))
        cases = content if isinstance(content, list) else [content]
        for case in cases:
            if "expr-file" in case:
                expr = (path / ".." / case["expr-file"]).resolve().read_text(encoding="utf-8")
            elif "expr" in case:
                expr = case["expr"]
            else:
                continue
            yield pytest.param(expr, id=f"{path.relative_to(SUITE_DIR)}::{expr[:40]!r}")


CASES = list(_iter_cases())


@pytest.mark.parametrize("expr", CASES)
def test_optimize_does_not_crash(expr: str) -> None:
    try:
        ast = Parser.parse(expr)
    except ParseError:
        return
    optimize(ast)


def test_constant_folding_examples() -> None:
    from jsonata2py.parser.ast_nodes import BooleanLiteral, NumberLiteral, StringLiteral

    assert optimize(Parser.parse("1 + 2")) == NumberLiteral(3)
    assert optimize(Parser.parse('"a" & "b"')) == StringLiteral("ab")
    assert optimize(Parser.parse("true and false")) == BooleanLiteral(False)
    assert optimize(Parser.parse("1 = 1")) == BooleanLiteral(True)
    assert optimize(Parser.parse("- -5")) == NumberLiteral(5)


def test_division_by_zero_not_folded() -> None:
    from jsonata2py.parser.ast_nodes import BinaryOp

    result = optimize(Parser.parse("1 / 0"))
    assert isinstance(result, BinaryOp)
    assert result.op == "/"
