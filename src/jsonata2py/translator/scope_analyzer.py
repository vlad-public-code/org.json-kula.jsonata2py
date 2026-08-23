"""Static AST analysis utilities for the code generator.

Ported from org.json_kula.jsonata_jvm.translator.ScopeAnalyzer.

These functions analyse the AST to determine which variables need
array-holder patterns, which outer-scope locals are free variables in a
block, and whether a given variable name appears free inside a subtree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..parser.ast_nodes import (
    ArrayConstructor,
    ArraySubscript,
    AstNode,
    BinaryOp,
    Block,
    ChainExpr,
    CoalesceExpr,
    ConditionalExpr,
    ContextBinding,
    ElvisExpr,
    ForceArray,
    FunctionCall,
    GroupByExpr,
    Lambda,
    LambdaCall,
    ObjectConstructor,
    Parenthesized,
    ParentStep,
    PartialApplication,
    PathExpr,
    PositionBinding,
    PredicateExpr,
    RangeExpr,
    SortExpr,
    TransformExpr,
    TransformLambda,
    UnaryMinus,
    VariableBinding,
    VariableRef,
)

if TYPE_CHECKING:
    from .gen_state import GenState


def compute_holder_needed(exprs: list[AstNode], block_local_names: set[str]) -> set[str]:
    """Returns the set of variable names in exprs that require an
    array-holder pattern: self-recursive lambdas, and forward references
    (a lambda at position i refers to a variable only bound at j > i)."""
    result: set[str] = set()

    bindings = [e for e in exprs if isinstance(e, VariableBinding)]
    binding_index = {vb.name: i for i, vb in enumerate(bindings)}

    for i, vb in enumerate(bindings):
        if not isinstance(vb.value, Lambda):
            continue
        lam = vb.value

        # Case 1: self-recursive -- body references the variable itself.
        if contains_var_ref(lam.body, vb.name):
            result.add(vb.name)

        # Case 2: forward reference -- body references a variable bound later.
        refs: set[str] = set()
        bound = set(lam.params)
        collect_free_vars_into(lam.body, refs, bound)
        for ref in refs:
            j = binding_index.get(ref)
            if j is not None and j > i:
                result.add(ref)
    return result


def collect_free_outer_vars(exprs: list[AstNode], block_locals: set[str], state: GenState) -> list[str]:
    """Returns the outer-scope local variable names that appear as free
    variables in exprs -- names in scope externally but not defined by the
    block itself. block_locals is intentionally unused in the free-var
    detection below (mirrors the Java deliberate choice), kept for parity
    with the call signature."""
    outer_locals: dict[str, None] = {}
    for scope in state.scope_stack:
        for name in scope:
            outer_locals[name] = None

    if not outer_locals:
        return []

    used: dict[str, None] = {}
    bound: set[str] = set()
    for expr in exprs:
        collect_free_vars_into(expr, used, bound)
        if isinstance(expr, VariableBinding):
            bound.add(expr.name)
    return [name for name in used if name in outer_locals]


def free_variables(root: AstNode) -> set[str]:
    """Returns every variable and function name the expression references
    but does not itself bind. Built-ins are not filtered out here; the
    caller decides what counts as provided."""
    used: dict[str, None] = {}
    collect_free_vars_into(root, used, set(), block_scope_is_whole_block=True)
    return set(used)


def collect_free_vars_into(
    node: AstNode | None,
    used: dict[str, None] | set[str],
    bound: set[str],
    block_scope_is_whole_block: bool = False,
) -> None:
    """block_scope_is_whole_block: when True, every name a block binds is
    in scope for the whole block, and path bindings are in scope for later
    steps."""
    if node is None:
        return

    def add(name: str) -> None:
        if isinstance(used, dict):
            used[name] = None
        else:
            used.add(name)

    def recurse(n: AstNode | None, b: set[str]) -> None:
        collect_free_vars_into(n, used, b, block_scope_is_whole_block)

    if isinstance(node, VariableRef):
        if node.name not in bound:
            add(node.name)
    elif isinstance(node, FunctionCall):
        if node.name not in bound:
            add(node.name)
        for a in node.args:
            recurse(a, bound)
    elif isinstance(node, Lambda):
        inner = bound | set(node.params)
        recurse(node.body, inner)
    elif isinstance(node, Block):
        inner = set(bound)
        if block_scope_is_whole_block:
            for e in node.expressions:
                _collect_binding_names(e, inner)
        for e in node.expressions:
            recurse(e, inner)
            if isinstance(e, VariableBinding):
                inner.add(e.name)
    elif isinstance(node, VariableBinding):
        recurse(node.value, bound)
    elif isinstance(node, BinaryOp):
        recurse(node.left, bound)
        recurse(node.right, bound)
    elif isinstance(node, UnaryMinus):
        recurse(node.operand, bound)
    elif isinstance(node, ConditionalExpr):
        recurse(node.condition, bound)
        recurse(node.then, bound)
        if node.otherwise is not None:
            recurse(node.otherwise, bound)
    elif isinstance(node, PathExpr):
        inner = set(bound) if block_scope_is_whole_block else bound
        for s in node.steps:
            if block_scope_is_whole_block and isinstance(s, (ContextBinding, PositionBinding)):
                inner.add(s.var_name)
            recurse(s, inner)
    elif isinstance(node, ArrayConstructor):
        for e in node.elements:
            recurse(e, bound)
    elif isinstance(node, ObjectConstructor):
        for p in node.pairs:
            recurse(p.key, bound)
            recurse(p.value, bound)
    elif isinstance(node, PredicateExpr):
        recurse(node.source, bound)
        recurse(node.predicate, bound)
    elif isinstance(node, ArraySubscript):
        recurse(node.source, bound)
        recurse(node.index, bound)
    elif isinstance(node, RangeExpr):
        recurse(node.from_, bound)
        recurse(node.to, bound)
    elif isinstance(node, SortExpr):
        recurse(node.source, bound)
        for k in node.keys:
            recurse(k.key, bound)
    elif isinstance(node, GroupByExpr):
        recurse(node.source, bound)
        for p in node.pairs:
            recurse(p.key, bound)
            recurse(p.value, bound)
    elif isinstance(node, ChainExpr):
        for s in node.steps:
            recurse(s, bound)
    elif isinstance(node, TransformExpr):
        recurse(node.source, bound)
        recurse(node.pattern, bound)
        recurse(node.update, bound)
        recurse(node.delete, bound)
    elif isinstance(node, ForceArray):
        recurse(node.source, bound)
    elif isinstance(node, Parenthesized):
        recurse(node.inner, bound)
    elif isinstance(node, (ElvisExpr, CoalesceExpr)):
        recurse(node.left, bound)
        recurse(node.right, bound)
    elif isinstance(node, PartialApplication):
        if node.name not in bound:
            add(node.name)
        for a in node.args:
            recurse(a, bound)
    # else: leaf nodes -- literals, FieldRef, WildcardStep, PartialPlaceholder, etc.


def contains_var_ref(node: AstNode, name: str) -> bool:
    """Returns True if node (or any sub-expression, respecting lambda
    parameter bindings) contains a reference to name as a free variable."""
    used: set[str] = set()
    collect_free_vars_into(node, used, set())
    return name in used


def contains_parent_step(node: AstNode | None, _cache: dict[int, bool] | None = None) -> bool:
    """Returns True if node contains a ParentStep anywhere in the subtree
    (including nested paths within constructors).

    _cache, when supplied, memoises by id(node) so overlapping subtrees
    visited repeatedly during one translation (the translator calls this
    on the same nodes from several call sites) are only walked once. It
    must be a fresh dict per translation, never a module-level one: id()
    is reused once a node is freed, so a longer-lived cache would be both
    a correctness hazard and a leak. GenState.contains_parent_step is the
    memoised entry point translator code should call; this function stays
    unmemoised by default for standalone/test callers."""
    if node is None:
        return False
    if _cache is not None:
        key = id(node)
        cached = _cache.get(key)
        if cached is not None:
            return cached
        result = _contains_parent_step_uncached(node, _cache)
        _cache[key] = result
        return result
    return _contains_parent_step_uncached(node, None)


def _contains_parent_step_uncached(node: AstNode, _cache: dict[int, bool] | None) -> bool:
    def rec(n: AstNode | None) -> bool:
        return contains_parent_step(n, _cache)

    if isinstance(node, ParentStep):
        return True
    if isinstance(node, PathExpr):
        return any(rec(s) for s in node.steps)
    if isinstance(node, ObjectConstructor):
        return any(rec(p.key) or rec(p.value) for p in node.pairs)
    if isinstance(node, ArrayConstructor):
        return any(rec(e) for e in node.elements)
    if isinstance(node, PredicateExpr):
        return rec(node.source) or rec(node.predicate)
    if isinstance(node, BinaryOp):
        return rec(node.left) or rec(node.right)
    if isinstance(node, ConditionalExpr):
        return rec(node.condition) or rec(node.then) or (node.otherwise is not None and rec(node.otherwise))
    if isinstance(node, Block):
        return any(rec(e) for e in node.expressions)
    if isinstance(node, FunctionCall):
        return any(rec(a) for a in node.args)
    if isinstance(node, Lambda):
        return rec(node.body)
    if isinstance(node, VariableBinding):
        return rec(node.value)
    if isinstance(node, SortExpr):
        return rec(node.source) or any(rec(k.key) for k in node.keys)
    if isinstance(node, ForceArray):
        return rec(node.source)
    if isinstance(node, GroupByExpr):
        return rec(node.source) or any(rec(p.key) or rec(p.value) for p in node.pairs)
    if isinstance(node, UnaryMinus):
        return rec(node.operand)
    if isinstance(node, Parenthesized):
        return rec(node.inner)
    # The branches below are easy to forget -- a missing one does not fail
    # loudly, it makes a '%' under that node type invisible to parent
    # tracking, and translation then fails with S0217 for a valid
    # expression. Keep this list in step with collect_free_vars_into above.
    if isinstance(node, RangeExpr):
        return rec(node.from_) or rec(node.to)
    if isinstance(node, ArraySubscript):
        return rec(node.source) or rec(node.index)
    if isinstance(node, ChainExpr):
        return any(rec(s) for s in node.steps)
    if isinstance(node, TransformExpr):
        return rec(node.source) or rec(node.pattern) or rec(node.update) or rec(node.delete)
    if isinstance(node, TransformLambda):
        return rec(node.pattern) or rec(node.update) or rec(node.delete)
    if isinstance(node, (ElvisExpr, CoalesceExpr)):
        return rec(node.left) or rec(node.right)
    if isinstance(node, PartialApplication):
        return any(rec(a) for a in node.args)
    if isinstance(node, LambdaCall):
        return rec(node.lambda_) or any(rec(a) for a in node.args)
    return False


def _collect_binding_names(expr: AstNode, out: set[str]) -> None:
    """Adds every name bound by expr, following chained assignments
    ($a := $b := v)."""
    current: AstNode = expr
    while isinstance(current, VariableBinding):
        out.add(current.name)
        current = current.value
