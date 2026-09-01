"""Capture-free per-element callbacks belong at module scope.

A predicate like `[level = 'senior']` compiles to a Python `lambda`. Left
inline, that lambda is a `MAKE_FUNCTION` inside the generated evaluator, so
it is *rebuilt on every evaluate() call* -- once per callback, forever, for
a function whose body can only ever reference its own parameter. The
benchmark expression alone rebuilt 18 of them per evaluation.

`gen_state.emit_callback` moves such a callback into the module-level
constant block that already holds hoisted key arrays, so it is built once
per compiled expression. Whether it may move is decided by a CLOSED
whitelist over the JSONata subtree (`gen_state._HOISTABLE`): an
unrecognised node type means "not hoistable". That direction of the test
matters most -- a false positive emits a module-level lambda referencing an
evaluator local, which is either a NameError or, worse, a silent read of
the wrong variable.

Ported from jsonata2js `translator.js#hoistableClosure`.
"""

from __future__ import annotations

import dis
import pathlib

import pytest

import jsonata2py as jsonata
from jsonata2py.parser.ast_nodes import AstNode
from jsonata2py.translator import gen_state

_FACTORY = jsonata.JsonataExpressionFactory()

_DATA = {
    "items": [
        {"id": 1, "name": "a", "price": 10, "qty": 2, "level": "junior", "sub": [{"k": 1}, {"k": 2}]},
        {"id": 2, "name": "b", "price": 30, "qty": 5, "level": "senior", "sub": [{"k": 3}]},
        {"id": 3, "name": "c", "price": 20, "qty": 1, "level": "senior", "sub": [{"k": 4}, {"k": 5}]},
    ],
    "cfg": {"limit": 15},
}


def _hoisted_constants(src: str) -> list[str]:
    """The module-level `_fnN = lambda ...` declarations, in emission order."""
    return [line for line in src.splitlines() if line.startswith("_fn")]


def _code_objects(src: str) -> dict[str, list]:
    """Every code object in the compiled generated module, by name."""
    out: dict[str, list] = {}
    stack = [compile(src, "<generated>", "exec")]
    while stack:
        code = stack.pop()
        out.setdefault(code.co_name, []).append(code)
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                stack.append(const)
    return out


def _opcount(code, opname: str) -> int:
    return sum(1 for ins in dis.get_instructions(code) if ins.opname == opname)


# =============================================================================
# What hoists
# =============================================================================

_HOISTS = [
    pytest.param("items[price > 15].name", id="comparison-predicate"),
    pytest.param("items[level = 'senior'].name", id="equality-predicate"),
    pytest.param("items[price >= 10 and qty < 5].name", id="and-predicate"),
    pytest.param("items[qty in [1, 2]].name", id="array-literal-predicate"),
    pytest.param("items[$string(id) = '2'].name", id="static-builtin-call"),
    pytest.param("items[$count(sub) = 2].name", id="static-builtin-over-path"),
    pytest.param("items[sub[k > 2]].name", id="nested-predicate"),
    pytest.param("items[price > 15 ? true : false].name", id="conditional-predicate"),
    pytest.param("items[-price < -15].name", id="unary-minus-predicate"),
    pytest.param("$count(items[price > 15])", id="fused-count-filter"),
    pytest.param("items^(price).name", id="sort-key"),
    pytest.param("items^(>price).name", id="descending-sort-key"),
    pytest.param("items.(price * qty)", id="mapped-block-step"),
    pytest.param("items.{'n': name}", id="object-constructor-step"),
    pytest.param("items.[name, level]", id="array-constructor-step"),
]


@pytest.mark.parametrize("expr", _HOISTS)
def test_capture_free_callback_moves_to_module_scope(expr: str) -> None:
    src = _FACTORY.translate(expr)
    assert _hoisted_constants(src), f"expected a hoisted callback in:\n{src}"
    # The whole point: nothing is left to rebuild per evaluation.
    for code in _code_objects(src)["_evaluate"]:
        assert _opcount(code, "MAKE_FUNCTION") == 0, f"evaluator still builds a closure:\n{src}"


@pytest.mark.parametrize("expr", _HOISTS)
def test_hoisting_preserves_the_result_across_repeated_evaluation(expr: str) -> None:
    """A hoisted callback is shared by every evaluate() call, so a stateful
    one would give a different answer the second time."""
    compiled = _FACTORY.compile(expr)
    first = compiled.evaluate(_DATA)
    assert compiled.evaluate(_DATA) == first
    assert compiled.evaluate(_DATA) == first


def test_transform_pattern_and_update_callbacks_both_hoist() -> None:
    """The `| pattern | update |` literal itself is a function *value*, so its
    own wrapper stays inline; the two per-location callbacks it passes to
    fn_transform are what get rebuilt per evaluation without hoisting."""
    src = _FACTORY.translate("items ~> |$|{'price': 99}|")
    assert _hoisted_constants(src) == [
        "_fn0 = lambda _p0: _p0",
        "_fn1 = lambda _p0: object_of(_keys0, [99])",
    ]
    assert "fn_transform(_ts0, _fn0, _fn1, MISSING)" in src


def test_hoisted_callbacks_use_canonical_parameter_names() -> None:
    """Minted `_el17`-style names would differ per occurrence and defeat the
    dedupe below; canonical `_p0` is what makes identical bodies identical."""
    src = _FACTORY.translate("items[price > 15].name")
    assert _hoisted_constants(src) == ["_fn0 = lambda _p0: gt(field(_p0, 'price'), 15)"]


def test_identical_predicates_share_one_hoisted_constant() -> None:
    src = _FACTORY.translate("items[level = 'senior'].name & items[level = 'senior'].id")
    assert len(_hoisted_constants(src)) == 1, f"expected one shared constant:\n{src}"
    assert _FACTORY.compile("items[level = 'senior'].name & items[level = 'senior'].id").evaluate(
        _DATA
    ) == "[\"b\",\"c\"][2,3]"


def test_benchmark_expression_builds_no_closures_per_evaluation() -> None:
    """The measured case: `_block0` held 18 MAKE_FUNCTION and no MAKE_CELL /
    LOAD_CLOSURE at all -- i.e. every one of them captured nothing."""
    expr = (
        pathlib.Path(__file__).resolve().parents[1] / "resources" / "benchmark" / "benchmark_expression.jsonata"
    ).read_text(encoding="utf-8")
    src = _FACTORY.translate(expr)
    codes = _code_objects(src)
    assert "_block0" in codes
    per_evaluation = [c for name, group in codes.items() if name != "<module>" for c in group]
    assert sum(_opcount(c, "MAKE_FUNCTION") for c in per_evaluation) == 0
    assert len(_hoisted_constants(src)) == 18


# =============================================================================
# What must NOT hoist
# =============================================================================

_STAYS_INLINE = [
    pytest.param(
        "($lim := 15; items[price > $lim].name)",
        "v_lim",
        id="binding-reference",
    ),
    pytest.param(
        "items.sub[%.price > 15].k",
        "_par",
        id="parent-step",
    ),
    pytest.param(
        "items[$$.cfg.limit < price].name",
        "_root",
        id="root-reference",
    ),
    pytest.param(
        "items[$count($filter(sub, function($s){ $s.k > 2 })) > 0].name",
        "v_s",
        id="nested-lambda",
    ),
    pytest.param(
        "($f := function($x){ $x.price > 15 }; items[$f($)].name)",
        "v_f",
        id="user-function-call",
    ),
    pytest.param(
        "($count := function($x){ 99 }; items[$count(sub) = 2].name)",
        "v_count",
        id="builtin-shadowed-by-a-binding",
    ),
    pytest.param(
        "items@$i[$i.price > 15].name",
        "v_i",
        id="context-binding",
    ),
    pytest.param(
        "items#$p[$p = 2].name",
        "_pair",
        id="position-binding",
    ),
    pytest.param(
        "items[$sort(sub, function($a,$b){ $a.k > $b.k })[0].k > 2].name",
        "_root",
        id="callback-taking-builtin",
    ),
    pytest.param(
        "items.sub^(%.k).k",
        "_par",
        id="parent-step-in-a-sort-key",
    ),
]


@pytest.mark.parametrize("expr,evaluator_local", _STAYS_INLINE)
def test_callback_reaching_the_evaluator_scope_stays_inline(expr: str, evaluator_local: str) -> None:
    src = _FACTORY.translate(expr)
    assert not _hoisted_constants(src), f"hoisted a callback that captures {evaluator_local}:\n{src}"
    # ...and the identifier it would have had to reach really is there, so a
    # hoist would have produced a module-level NameError rather than a
    # harmless duplicate.
    assert evaluator_local in src, f"expected {evaluator_local} in:\n{src}"


@pytest.mark.parametrize("expr,_local", _STAYS_INLINE)
def test_inline_callbacks_still_evaluate(expr: str, _local: str) -> None:
    """Guards the other failure mode of the canonical-name rewrite: an inline
    callback nested inside a hoist candidate must not shadow the parameter of
    the callback it lives in."""
    compiled = _FACTORY.compile(expr)
    assert compiled.evaluate(_DATA) == compiled.evaluate(_DATA)


def test_generated_module_never_mentions_a_hoisted_name_it_did_not_declare() -> None:
    """The property that makes a false positive impossible to hide: every
    `_fnN` referenced from the evaluator must be declared at module level."""
    for expr in [p.values[0] for p in (*_HOISTS, *_STAYS_INLINE)]:
        src = _FACTORY.translate(str(expr))
        declared = {line.split(" = ", 1)[0] for line in _hoisted_constants(src)}
        module_code = _code_objects(src)["<module>"][0]
        used = {
            ins.argval
            for code in _code_objects(src)
            if code != "<module>"
            for c in _code_objects(src)[code]
            for ins in dis.get_instructions(c)
            if ins.opname == "LOAD_GLOBAL" and isinstance(ins.argval, str) and ins.argval.startswith("_fn")
        }
        assert used <= declared, f"{expr}: undeclared {used - declared}"
        assert declared <= set(module_code.co_names), f"{expr}: {declared} not assigned at module level"


# =============================================================================
# The whitelist itself
# =============================================================================


def test_whitelist_is_closed_against_unknown_node_types() -> None:
    class NewNode(AstNode):
        __slots__ = ()

    assert gen_state._hoistable(NewNode(), gen_state.GenState()) is False


@pytest.mark.parametrize(
    "class_name",
    [
        "RootRef",
        "VariableRef",
        "ParentStep",
        "ContextBinding",
        "PositionBinding",
        "Lambda",
        "LambdaCall",
        "PartialApplication",
        "PartialPlaceholder",
        "Block",
        "VariableBinding",
        "SortExpr",
        "GroupByExpr",
        "ChainExpr",
        "TransformExpr",
        "TransformLambda",
    ],
)
def test_scope_reaching_node_types_are_absent_from_the_whitelist(class_name: str) -> None:
    assert class_name not in {cls.__name__ for cls in gen_state._HOISTABLE}


@pytest.mark.parametrize("name", ["sort", "map", "filter", "each", "reduce", "single", "sift"])
def test_callback_taking_builtins_are_not_static(name: str) -> None:
    """These compile to a named helper `def` called with `_root` threaded in
    as an argument, so a call to one can never sit in a hoisted body."""
    assert name not in gen_state._STATIC_BUILTINS
