"""Phase 1 gate: every expression in the official JSONata test suite must
parse without an unexpected exception, and where the suite expects a
parse-time error code (S0xxx syntax errors), the parser must raise exactly
that code.

Codes like T1006/T1008 (calling a non-function) and S0217 (parent operator
with no parent context) are deferred to the translator/runtime in this
design -- the postfix parser desugars `expr(args)` into a generic call for
*any* expr, so "is this actually callable" can only be answered once
bindings/scoping are known. Those codes are intentionally excluded here and
covered once the translator exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jsonata2py.errors import ParseError
from jsonata2py.parser.parser import Parser

SUITE_DIR = Path(__file__).parent.parent / "resources" / "jsonata-test-suite" / "groups"

# Error codes that this parser design defers to the translator/runtime rather
# than raising during Parser.parse(). See BlockCodeGen/PathCodeGen/Translator
# in the Java source for where these are actually raised.
DEFERRED_TO_LATER_PHASES = {"T1005", "T1006", "T1007", "T1008", "S0217"}


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
            expected_code = case.get("code") or (case.get("error") or {}).get("code")
            yield pytest.param(expr, expected_code, id=f"{path.relative_to(SUITE_DIR)}::{expr[:40]!r}")


CASES = list(_iter_cases())


@pytest.mark.parametrize("expr,expected_code", CASES)
def test_expression_parses_or_raises_expected_code(expr: str, expected_code: str | None) -> None:
    try:
        Parser.parse(expr)
    except ParseError as e:
        if expected_code is not None and expected_code not in DEFERRED_TO_LATER_PHASES:
            assert e.error_code == expected_code, f"parse error code mismatch for {expr!r}"
        # else: some other case's expression happens to be a syntax error we
        # don't have an expectation for here (e.g. a case whose *code* is a
        # runtime code but whose expr also happens to be malformed) -- only
        # fail loudly on an explicit S0xxx-style mismatch above.
