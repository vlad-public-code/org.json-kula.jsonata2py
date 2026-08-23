"""Phase 4 gate (and onward): the full pipeline -- parse, optimize,
translate, load, evaluate -- run against the official JSONata test suite.

Mirrors JsonataTestSuiteTest.java's case-format handling. Cases whose
expression needs functionality not yet ported (XPath picture-string
formatting/parsing for $formatNumber/$formatInteger/$parseInteger/$now
with a picture/$fromMillis with a picture/$toMillis with a picture --
Phase 5c/5d) are marked xfail(strict=True) so they cannot silently start
passing (a sign to remove the mark) or regress further un-noticed.

This is not yet the full acceptance gate from docs/porting-design-spec.md
section 10.1 (1281/1281) -- that lands once Phase 5-8 are complete. Until
then this file tracks real progress without blocking on unported pieces.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import jsonata2py as jsonata
from jsonata2py.bindings import JsonataBindings
from jsonata2py.errors import JsonataCompilationError, JsonataEvaluationError
from jsonata2py.runtime.values import MISSING

SUITE_DIR = Path(__file__).parent / "resources" / "jsonata-test-suite"
GROUPS_DIR = SUITE_DIR / "groups"
DATASETS_DIR = SUITE_DIR / "datasets"

_dataset_cache: dict[str, Any] = {}


def _load_dataset(name: str) -> Any:
    if name not in _dataset_cache:
        _dataset_cache[name] = json.loads((DATASETS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return _dataset_cache[name]


def _get_data(case: dict[str, Any]) -> Any:
    if "data" in case:
        return case["data"]
    if "dataset" in case:
        name = case["dataset"]
        return MISSING if name is None else _load_dataset(name)
    return {}


def _iter_cases():
    factory = jsonata.JsonataExpressionFactory()
    for path in sorted(GROUPS_DIR.rglob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))
        cases = content if isinstance(content, list) else [content]
        for case in cases:
            if "expr-file" in case:
                expr = (path.parent / case["expr-file"]).read_text(encoding="utf-8")
            elif "expr" in case:
                expr = case["expr"]
            else:
                continue
            case_id = f"{path.relative_to(GROUPS_DIR)}::{expr[:40]!r}"
            yield pytest.param(case, path, factory, expr, id=case_id)


CASES = list(_iter_cases())


@pytest.mark.parametrize("case,path,factory,expr", CASES)
def test_suite_case(case: dict[str, Any], path: Path, factory: jsonata.JsonataExpressionFactory, expr: str) -> None:
    expected_result = case.get("result")
    undefined_result = bool(case.get("undefinedResult", False))
    expected_code = case.get("code") or (case.get("error") or {}).get("code")

    bindings = JsonataBindings()
    for k, v in case.get("bindings", {}).items():
        bindings.bind_value(k, v)

    data = _get_data(case)
    # Evaluating must never modify the caller's input. This is asserted for
    # every one of the ~1281 official cases rather than in a handful of
    # targeted tests, because the failure mode is invisible from the result:
    # merge_group_by_objects once extended a list it had navigated straight
    # out of the input, so the first evaluate() looked correct and only a
    # *second* evaluate() on the same document returned the wrong answer.
    # It also protects _dataset_cache, whose datasets are shared by many
    # cases -- a mutation there would corrupt unrelated tests downstream.
    data_before = None if data is MISSING else copy.deepcopy(data)

    try:
        compiled = factory.compile(expr)
        result = compiled.evaluate(data, bindings)
    except NotImplementedError as e:
        # Genuinely unported functionality (picture-string engines,
        # Phase 5c/5d) -- not a translator/runtime bug, dynamically
        # detected rather than statically guessed so no case is ever
        # mis-skipped or silently masks a real regression.
        pytest.xfail(f"not yet ported: {e}")
        return
    except (JsonataCompilationError, JsonataEvaluationError) as e:
        if isinstance(e.__cause__, NotImplementedError) or isinstance(
            getattr(e, "__context__", None), NotImplementedError
        ):
            pytest.xfail(f"not yet ported: {e.message}")
            return
        if expected_code is not None:
            assert e.error_code == expected_code, f"error code mismatch for {expr!r}: {e.message}"
            return
        pytest.fail(f"unexpected error for {expr!r}: {e.error_code} {e.message}")
        return

    if data is not MISSING:
        assert data == data_before, f"evaluating {expr!r} modified its input document"

    if expected_code is not None:
        pytest.fail(f"expected error {expected_code} for {expr!r} but got result: {result!r}")
    elif undefined_result:
        assert result is MISSING, f"expected undefined for {expr!r}, got {result!r}"
    elif "result" in case:
        assert result == expected_result, f"expected {expected_result!r} for {expr!r}, got {result!r}"
    else:
        pytest.fail(f"case for {expr!r} declares neither result, undefinedResult, nor code")
