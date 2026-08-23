"""Code generation for path expressions -- the largest and most intricate
part of the translator.

Ported from org.json_kula.jsonata_jvm.translator.PathCodeGen.

A JSONata path is not a simple chain of field reads. Each step may rebind
the context, map over a sequence, filter it, keep a reference to its
parent for %, cross-join through a context binding (@$v), or construct a
value that must not be flattened into the surrounding sequence -- and the
code emitted for one step depends on what the following steps do.

Structural divergence from Java (documented): several of Java's path-step
handlers build an anonymous *block-bodied* closure -- `(pair -> { JsonNode
$v = pair.get(0); JsonNode $i = pair.get(1); return rest; })` -- to unpack
an (element, index) tuple before evaluating the rest of the path. Python
lambdas cannot contain statements, but the unpacked names are used
*read-only* and side-effect-free (list indexing), so instead of emitting
statements this port registers each unpacked name as a GenState *alias*
resolving directly to the inline indexing expression (`_pair[0] if
isinstance(_pair, list) else _pair`, etc.) before compiling the rest of
the path. Every later reference to that JSONata variable/context then
inlines the indexing expression wherever it's used, and the whole
construct stays a genuine single-expression Python `lambda`, which is
still a real closure over the enclosing generated function's locals --
exactly matching Java's closure semantics, just via a different
mechanical trick forced by Python's syntax.

One case (the #$pos-after-outer-bindings global counter) is genuinely
stateful (a counter must be incremented exactly once per invocation, not
once per reference) -- that one uses `next_counter(box)` as a pure-expression
post-increment (see runtime/core.py) rather than a statement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..errors import TranslatorError
from ..parser.ast_nodes import (
    ArrayConstructor,
    ArraySubscript,
    AstNode,
    BinaryOp,
    BooleanLiteral,
    ContextBinding,
    ContextRef,
    DescendantStep,
    FieldRef,
    ForceArray,
    FunctionCall,
    GroupByExpr,
    ObjectConstructor,
    Parenthesized,
    ParentStep,
    PathExpr,
    PositionBinding,
    PredicateExpr,
    RangeExpr,
    RootRef,
    SortExpr,
    StringLiteral,
    WildcardStep,
    accept,
)
from .naming import py_string, pyvar

if TYPE_CHECKING:
    from .gen_ctx import GenCtx
    from .translator import Translator


def _pair_elem_expr(pair_var: str) -> str:
    return f"({pair_var}[0] if isinstance({pair_var}, list) else {pair_var})"


def _pair_idx_expr(pair_var: str) -> str:
    return f"({pair_var}[1] if isinstance({pair_var}, list) else 0)"


def visit_path_expr(t: Translator, n: PathExpr, ctx: GenCtx) -> str:
    steps = list(n.steps)

    # Each PathExpr is a fresh path scope: clear any inherited cross-join
    # context so sub-paths inside constructors/predicates don't reuse an
    # outer cross-join base.
    ctx = ctx.with_cross_join_parent(None)

    first_step = steps[0]
    force_arr = isinstance(first_step, ForceArray)
    if isinstance(first_step, ForceArray):
        first_step = first_step.source
    if not force_arr and isinstance(first_step, PredicateExpr) and isinstance(first_step.source, ForceArray):
        force_arr = True

    if force_arr and isinstance(first_step, PathExpr) and _has_any_binding(first_step.steps) and len(steps) > 1:
        merged_steps = [*first_step.steps, *steps[1:]]
        return f"force_array({visit_path_expr(t, PathExpr(merged_steps), ctx)})"

    if (
        not force_arr
        and isinstance(first_step, PredicateExpr)
        and isinstance(first_step.source, PathExpr)
        and _has_any_binding(first_step.source.steps)
    ):
        new_steps = [*first_step.source.steps, PredicateExpr(ContextRef(), first_step.predicate), *steps[1:]]
        return visit_path_expr(t, PathExpr(new_steps), ctx)

    if (
        not force_arr
        and isinstance(first_step, SortExpr)
        and isinstance(first_step.source, PathExpr)
        and _has_any_binding(first_step.source.steps)
    ):
        inner_steps = first_step.source.steps
        bind_idx = -1
        for i, s in enumerate(inner_steps):
            if isinstance(s, (ContextBinding, PositionBinding)):
                bind_idx = i
        new_steps = list(inner_steps[: bind_idx + 1])
        sort_source_steps = list(inner_steps[bind_idx + 1 :])
        sort_source: AstNode = (
            ContextRef()
            if not sort_source_steps
            else sort_source_steps[0] if len(sort_source_steps) == 1 else PathExpr(sort_source_steps)
        )
        new_steps.append(SortExpr(sort_source, first_step.keys))
        new_steps.extend(steps[1:])
        return visit_path_expr(t, PathExpr(new_steps), ctx)

    if _has_cross_join_field_ref(steps) and _needs_parent_tracking(steps, 0, ctx) and not ctx.parent_vars:
        ctx = ctx.with_parents([ctx.ctx_var])

    start_from = 1
    if isinstance(first_step, ParentStep):
        if not ctx.parent_vars:
            raise TranslatorError("S0217", "Parent operator % used with no parent context")
        expr = ctx.parent_vars[-1]
        new_parents = ctx.parent_vars[:-1]
        ctx = ctx.with_parents(new_parents)
    else:
        expr = _step_expr(t, first_step, ctx)

    if _has_context_binding(steps) and not force_arr:
        for si in range(1, len(steps)):
            hoist_as = steps[si]
            if (
                isinstance(hoist_as, ArraySubscript)
                and isinstance(hoist_as.source, PredicateExpr)
                and si > 0
                and isinstance(steps[si - 1], (ContextBinding, PositionBinding, PredicateExpr))
            ):
                hoisted_steps = list(steps)
                hoisted_steps[si] = hoist_as.source
                inner_result = visit_path_expr(t, PathExpr(hoisted_steps), ctx)
                idx_expr = accept(hoist_as.index, t, ctx)
                return f"subscript({inner_result}, {idx_expr})"

    result = _compile_path_steps(t, steps, start_from, expr, ctx)
    if _path_ends_with_group_by_after_binding(steps):
        result = f"merge_group_by_objects({result})"
    return f"force_array({result})" if force_arr else result


def _path_ends_with_group_by_after_binding(steps: list[AstNode]) -> bool:
    if len(steps) < 2:
        return False
    last = steps[-1]
    if not (isinstance(last, GroupByExpr) and isinstance(last.source, ContextRef)):
        return False
    return any(isinstance(s, (ContextBinding, PositionBinding)) for s in steps[:-1])


def _has_cross_join_field_ref(steps: list[AstNode]) -> bool:
    n = len(steps)
    for i in range(n - 1):
        if isinstance(steps[i], ContextBinding) and isinstance(steps[i + 1], FieldRef):
            return True
        if (
            isinstance(steps[i], ContextBinding)
            and i + 2 < n
            and isinstance(steps[i + 1], PositionBinding)
            and isinstance(steps[i + 2], FieldRef)
        ):
            return True
        if (
            isinstance(steps[i], ContextBinding)
            and i + 2 < n
            and isinstance((nxt := _step_at(steps, i + 1)), SortExpr)
            and isinstance(nxt.source, ContextRef)
            and isinstance(_step_at(steps, i + 2), FieldRef)
        ):
            return True
    return False


def _has_context_binding(steps: list[AstNode]) -> bool:
    return any(isinstance(s, ContextBinding) for s in steps)



def _step_at(steps: list[AstNode], i: int) -> AstNode | None:
    """`steps[i]`, or None when `i` is out of range.

    Lets a caller bind the step once and isinstance-narrow it, instead of
    re-indexing inside a boolean chain -- which reads worse and gives the
    type checker nothing to narrow.
    """
    return steps[i] if 0 <= i < len(steps) else None

def _has_any_binding(steps: list[AstNode]) -> bool:
    return any(isinstance(s, (ContextBinding, PositionBinding)) for s in steps)


def _compile_path_steps(t: Translator, steps: list[AstNode], from_: int, prev_expr: str, ctx: GenCtx) -> str:
    if from_ >= len(steps):
        return prev_expr

    step = steps[from_]

    context_bound = _compile_context_binding_step(t, steps, from_, prev_expr, ctx)
    if context_bound is not None:
        return context_bound

    position_bound = _compile_position_binding_step(t, steps, from_, prev_expr, ctx)
    if position_bound is not None:
        return position_bound

    if isinstance(step, ParentStep):
        parents = ctx.parent_vars
        if not parents:
            raise TranslatorError("S0217", "Parent operator % used with no parent context in path")
        parent_expr = parents[-1]
        new_parents = parents[:-1]
        guard_var = f"_gu{ctx.state.next_id()}"
        guarded_expr = f"map_step({prev_expr}, lambda {guard_var}: {parent_expr})"
        return _compile_path_steps(t, steps, from_ + 1, guarded_expr, ctx.with_parents(new_parents))

    needs_tracking = _needs_parent_tracking(steps, from_, ctx)
    if needs_tracking and isinstance(step, FieldRef):
        elem_var = f"_el{ctx.state.next_id()}"

        if ctx.cross_join_parent is not None:
            parent_var = ctx.cross_join_parent
            new_parents = (*ctx.parent_vars, parent_var)
            field_expr = f"field({parent_var}, {py_string(step.name)})"
            dummy_var = f"_dc{ctx.state.next_id()}"
            inner_ctx = ctx.with_ctx(elem_var).with_parents(new_parents).with_cross_join_parent(parent_var)
            rest_expr = _compile_path_steps(t, steps, from_ + 1, elem_var, inner_ctx)
            return f"map_step({prev_expr}, lambda {dummy_var}: map_step({field_expr}, lambda {elem_var}: {rest_expr}))"

        if (
            from_ + 1 < len(steps)
            and isinstance(steps[from_ + 1], ContextBinding)
            and from_ + 2 < len(steps)
            and isinstance(steps[from_ + 2], PositionBinding)
        ):
            parent_var = f"_par{ctx.state.next_id()}"
            new_parents = (*ctx.parent_vars, parent_var)
            field_expr = f"field({parent_var}, {py_string(step.name)})"
            inner_ctx = ctx.with_ctx(field_expr).with_parents(new_parents).with_cross_join_parent(parent_var)
            rest_expr = _compile_path_steps(t, steps, from_ + 1, field_expr, inner_ctx)
            return f"map_step({prev_expr}, lambda {parent_var}: {rest_expr})"

        parent_var = f"_par{ctx.state.next_id()}"
        new_parents = (*ctx.parent_vars, parent_var)
        field_expr = f"field({parent_var}, {py_string(step.name)})"
        inner_ctx = ctx.with_ctx(elem_var).with_parents(new_parents)
        rest_expr = _compile_path_steps(t, steps, from_ + 1, elem_var, inner_ctx)
        return f"map_step({prev_expr}, lambda {parent_var}: map_step({field_expr}, lambda {elem_var}: {rest_expr}))"

    if (
        not needs_tracking
        and ctx.cross_join_parent is not None
        and ctx.cross_join_parent != prev_expr
        and isinstance(step, FieldRef)
    ):
        cj_parent = ctx.cross_join_parent
        field_expr = f"field({cj_parent}, {py_string(step.name)})"
        dummy_var = f"_dc{ctx.state.next_id()}"
        inner_ctx = ctx.with_cross_join_parent(cj_parent)
        rest_expr = _compile_path_steps(t, steps, from_ + 1, field_expr, inner_ctx)
        return f"map_step({prev_expr}, lambda {dummy_var}: {rest_expr})"

    new_expr = apply_step(t, prev_expr, step, ctx)
    return _compile_path_steps(t, steps, from_ + 1, new_expr, ctx)


def _has_outer_bindings(steps: list[AstNode], up_to: int) -> bool:
    return any(isinstance(steps[i], (ContextBinding, PositionBinding)) for i in range(up_to))


def _needs_parent_tracking(steps: list[AstNode], from_: int, ctx: GenCtx) -> bool:
    n = len(steps)
    for i in range(from_, n):
        if ctx.state.contains_parent_step(steps[i]):
            return True
        if isinstance(steps[i], ContextBinding) and i + 1 < n and isinstance(steps[i + 1], FieldRef):
            return True
        if (
            isinstance(steps[i], ContextBinding)
            and i + 2 < n
            and isinstance(steps[i + 1], PositionBinding)
            and isinstance(steps[i + 2], FieldRef)
        ):
            return True
        if (
            isinstance(steps[i], ContextBinding)
            and i + 2 < n
            and isinstance((nxt := _step_at(steps, i + 1)), SortExpr)
            and isinstance(nxt.source, ContextRef)
            and isinstance(_step_at(steps, i + 2), FieldRef)
        ):
            return True
    return False


def path_ends_with_array_constructor(node: AstNode) -> bool:
    if isinstance(node, PathExpr):
        return bool(node.steps) and isinstance(node.steps[-1], ArrayConstructor)
    return False


def _step_expr(t: Translator, step: AstNode, ctx: GenCtx) -> str:
    """Generates an expression for the FIRST step in a path (uses
    ctx.ctx_var)."""
    if isinstance(step, FieldRef):
        return f"field({ctx.ctx_var}, {py_string(step.name)})"
    if isinstance(step, StringLiteral):
        # A quoted string as the leading path step is a field reference.
        return f"field({ctx.ctx_var}, {py_string(step.value)})"
    if isinstance(step, WildcardStep):
        return f"wildcard({ctx.ctx_var})"
    if isinstance(step, DescendantStep):
        return f"descendant({ctx.ctx_var})"
    if isinstance(step, ContextRef):
        return ctx.ctx_var
    if isinstance(step, RootRef):
        return ctx.root_var
    return accept(step, t, ctx)


def apply_step(t: Translator, prev_expr: str, step: AstNode, ctx: GenCtx) -> str:
    """Generates an expression that applies step to prev_expr (steps 2+ in
    a path)."""
    if isinstance(step, FieldRef):
        return f"field({prev_expr}, {py_string(step.name)})"
    if isinstance(step, WildcardStep):
        return f"wildcard({prev_expr})"
    if isinstance(step, DescendantStep):
        return f"descendant({prev_expr})"
    if isinstance(step, ContextRef):
        return prev_expr
    if isinstance(step, RootRef):
        return ctx.root_var
    if isinstance(step, PredicateExpr):
        if isinstance(step.predicate, RangeExpr):
            from_expr = accept(step.predicate.from_, t, ctx)
            to_expr = accept(step.predicate.to, t, ctx)
            return f"range_subscript({prev_expr}, {from_expr}, {to_expr})"
        elem_var = f"_el{ctx.state.next_id()}"
        pred_expr = accept(step.predicate, t, ctx.with_ctx(elem_var))
        return f"dynamic_filter({prev_expr}, lambda {elem_var}: {pred_expr})"
    if isinstance(step, ArraySubscript):
        tmp_ctx = f"_c{ctx.state.next_id()}"
        src_expr = accept(step.source, t, ctx.with_ctx(tmp_ctx))
        idx_expr = accept(step.index, t, ctx.with_ctx(tmp_ctx))
        return f"map_step({prev_expr}, lambda {tmp_ctx}: subscript({src_expr}, {idx_expr}))"
    if isinstance(step, ArrayConstructor):
        tmp_ctx = f"_c{ctx.state.next_id()}"
        step_expr = accept(step, t, ctx.with_ctx(tmp_ctx).with_in_array_constructor_step())
        call = f"map_constructor_step({prev_expr}, lambda {tmp_ctx}: {step_expr})"
        return call if ctx.array_constructor_preserve else f"unwrap({call})"
    if isinstance(step, ObjectConstructor):
        if ctx.tuple_pos is not None:
            # tuple_pos carries the *raw JSONata* variable name (e.g. "pos",
            # not "v_pos") bound by a preceding position-aware global sort;
            # elements arrive here as (item, $pos) tuples. Alias the name
            # to the pair's index slot -- scoped to this object body only --
            # instead of Java's approach of declaring a fresh local, which
            # Python's expression-only lambdas can't do.
            tmp_tuple = f"_c{ctx.state.next_id()}"
            t_pos_expr = _pair_idx_expr(tmp_tuple)
            t_elem_expr = _pair_elem_expr(tmp_tuple)
            pos_name = ctx.tuple_pos
            ctx.state.push_scope()
            ctx.state.add_local_var_with_alias(pos_name, t_pos_expr)
            try:
                step_expr = accept(step, t, ctx.with_ctx(t_elem_expr).with_tuple_pos(None))
            finally:
                ctx.state.pop_scope()
            return f"unwrap(map_constructor_step({prev_expr}, lambda {tmp_tuple}: {step_expr}))"
        tmp_ctx = f"_c{ctx.state.next_id()}"
        step_expr = accept(step, t, ctx.with_ctx(tmp_ctx))
        return f"unwrap(map_constructor_step({prev_expr}, lambda {tmp_ctx}: {step_expr}))"
    if isinstance(step, GroupByExpr) and isinstance(step.source, ContextRef):
        # GroupByExpr(ContextRef) appears as a path step only after Rule D's
        # binding rewrite. It must receive the whole accumulated sequence,
        # not be applied per-element.
        return accept(step, t, ctx.with_ctx(prev_expr))
    # Default: rebind the context to prev_expr inside a mapped lambda.
    tmp_ctx = f"_c{ctx.state.next_id()}"
    if ctx.state.contains_parent_step(step) and not ctx.parent_vars:
        inner_ctx = ctx.with_ctx(tmp_ctx).with_parents([ctx.ctx_var])
    else:
        inner_ctx = ctx.with_ctx(tmp_ctx)
    step_expr = accept(step, t, inner_ctx)
    return f"map_step({prev_expr}, lambda {tmp_ctx}: {step_expr})"


_STATIC_BOOLEAN_OPS = {"=", "!=", "<", ">", "<=", ">=", "in", "and", "or"}
_STATIC_BOOLEAN_FUNCS = {"boolean", "not", "exists", "contains"}


def _is_static_boolean_predicate(node: AstNode) -> bool:
    """True if the predicate is statically guaranteed to produce a boolean
    (never a number), so filter_() can be used instead of dynamic_filter()."""
    if isinstance(node, BinaryOp):
        return node.op in _STATIC_BOOLEAN_OPS
    if isinstance(node, BooleanLiteral):
        return True
    if isinstance(node, FunctionCall):
        return node.name in _STATIC_BOOLEAN_FUNCS
    if isinstance(node, Parenthesized):
        return _is_static_boolean_predicate(node.inner)
    return False


def visit_predicate_expr(t: Translator, n: PredicateExpr, ctx: GenCtx) -> str:
    source_node = n.source
    force_arr = isinstance(source_node, ForceArray)
    if isinstance(source_node, ForceArray):
        source_node = source_node.source

    if isinstance(source_node, PathExpr) and source_node.steps:
        last_step = source_node.steps[-1]
        if isinstance(last_step, (PositionBinding, ContextBinding)):
            new_steps = [*source_node.steps, PredicateExpr(ContextRef(), n.predicate)]
            new_source: AstNode = PathExpr(new_steps)
            wrapped: AstNode = ForceArray(new_source) if force_arr else new_source
            return accept(wrapped, t, ctx)

    if isinstance(n.predicate, RangeExpr):
        re = n.predicate
        if (
            not force_arr
            and isinstance(source_node, PathExpr)
            and source_node.steps
            and isinstance(source_node.steps[-1], ContextRef)
        ):
            base_steps = source_node.steps[:-1]
            base_source: AstNode = (
                ContextRef() if not base_steps else base_steps[0] if len(base_steps) == 1 else PathExpr(base_steps)
            )
            spread_expr = accept(base_source, t, ctx)
            from_expr = accept(re.from_, t, ctx)
            to_expr = accept(re.to, t, ctx)
            elem_var = f"_me{ctx.state.next_id()}"
            return f"map_step({spread_expr}, lambda {elem_var}: range_subscript({elem_var}, {from_expr}, {to_expr}))"
        src_expr = accept(source_node, t, ctx)
        from_expr = accept(re.from_, t, ctx)
        to_expr = accept(re.to, t, ctx)
        r = f"range_subscript({src_expr}, {from_expr}, {to_expr})"
        return f"force_array({r})" if force_arr else r

    if ctx.state.contains_parent_step(n.predicate):
        collected_preds: list[AstNode] = []
        base_path = _extract_base_path_and_predicates(source_node, collected_preds)
        if base_path is not None:
            collected_preds.append(n.predicate)
            new_steps = list(base_path.steps)
            for pred in collected_preds:
                new_steps.append(PredicateExpr(ContextRef(), pred))
            result = accept(PathExpr(new_steps), t, ctx)
            return f"force_array({result})" if force_arr else result

    src_expr = accept(source_node, t, ctx)
    elem_var = f"_el{ctx.state.next_id()}"
    pred_expr = accept(n.predicate, t, ctx.with_ctx(elem_var))
    filter_fn = "filter_" if _is_static_boolean_predicate(n.predicate) else "dynamic_filter"
    r = f"{filter_fn}({src_expr}, lambda {elem_var}: {pred_expr})"
    return f"force_array({r})" if force_arr else r


def _extract_base_path_and_predicates(source: AstNode, collected_preds: list[AstNode]) -> PathExpr | None:
    if isinstance(source, PathExpr):
        return source
    if isinstance(source, PredicateExpr):
        base = _extract_base_path_and_predicates(source.source, collected_preds)
        if base is not None:
            collected_preds.append(source.predicate)
            return base
    return None


# =============================================================================
# @$v context binding
# =============================================================================


def _compile_context_binding_step(
    t: Translator, steps: list[AstNode], from_: int, prev_expr: str, ctx: GenCtx
) -> str | None:
    step = steps[from_]
    if not isinstance(step, ContextBinding):
        return None
    cb = step
    # Matches Java exactly: "$" + varName is reused directly as the lambda
    # parameter name, so a plain (unaliased) local registration already
    # makes $e resolve to the same identifier the lambda binds -- no alias
    # bookkeeping needed for this, the common, case.
    var_name = pyvar(cb.var_name)
    ctx.state.push_scope()
    ctx.state.add_local_var(cb.var_name)
    try:
        n = len(steps)
        if isinstance((paren := _step_at(steps, from_ + 1)), Parenthesized):
            # Cross-join: the parenthesized expression evaluates from document root.
            inner_expr = accept(paren.inner, t, ctx.with_ctx(ctx.root_var))
            inner_ctx = ctx.with_ctx(var_name).with_parents([var_name])
            rest_expr = _compile_path_steps(t, steps, from_ + 2, inner_expr, inner_ctx)
            return f"map_step({prev_expr}, lambda {var_name}: {rest_expr})"

        if (
            from_ + 1 < n
            and isinstance((pb2 := _step_at(steps, from_ + 1)), PositionBinding)
            and from_ + 2 < n
            and isinstance(_step_at(steps, from_ + 2), FieldRef)
        ):
            # @$var#$pos.FieldRef cross-join.
            pair_var = f"_pair{ctx.state.next_id()}"
            elem_expr = _pair_elem_expr(pair_var)
            idx_expr = _pair_idx_expr(pair_var)
            ctx.state.add_local_var_with_alias(cb.var_name, elem_expr)
            ctx.state.add_local_var_with_alias(pb2.var_name, idx_expr)
            # $e itself (elem_expr) is the fallback cross-join base when no
            # other parent context is established -- matches Java's use of
            # varName (there, a real bound local) for the same fallback.
            cjp = ctx.cross_join_parent if ctx.cross_join_parent is not None else (
                elem_expr if not ctx.parent_vars else ctx.parent_vars[-1]
            )
            inner_ctx = ctx.with_ctx(elem_expr).with_cross_join_parent(cjp)
            rest_expr = _compile_path_steps(t, steps, from_ + 2, cjp, inner_ctx)
            return f"each_indexed({prev_expr}, lambda {pair_var}: {rest_expr})"

        if isinstance((pb3 := _step_at(steps, from_ + 1)), PositionBinding):
            # @$var#$pos without a following cross-join FieldRef.
            pair_var = f"_pair{ctx.state.next_id()}"
            elem_expr = _pair_elem_expr(pair_var)
            idx_expr = _pair_idx_expr(pair_var)
            ctx.state.add_local_var_with_alias(cb.var_name, elem_expr)
            ctx.state.add_local_var_with_alias(pb3.var_name, idx_expr)
            popped_parents = ctx.parent_vars[:-1] if ctx.parent_vars else ()
            inner_ctx = ctx.with_ctx(elem_expr).with_parents(popped_parents).with_cross_join_parent(
                ctx.cross_join_parent
            )
            rest_expr = _compile_path_steps(t, steps, from_ + 2, elem_expr, inner_ctx)
            return f"each_indexed({prev_expr}, lambda {pair_var}: {rest_expr})"

        if from_ + 1 < n and isinstance(steps[from_ + 1], FieldRef):
            # Cross-join: @$var followed by a FieldRef navigates from the
            # cross-join parent (last in parent_vars), not from each $var.
            cjp = var_name if not ctx.parent_vars else ctx.parent_vars[-1]
            inner_ctx = ctx.with_ctx(var_name).with_cross_join_parent(cjp)
            rest_expr = _compile_path_steps(t, steps, from_ + 1, cjp, inner_ctx)
            return f"map_step({prev_expr}, lambda {var_name}: {rest_expr})"

        if (
            isinstance((se := _step_at(steps, from_ + 1)), SortExpr)
            and isinstance(se.source, ContextRef)
        ):
            # @$var ^(sort) more_steps: sort the WHOLE source first, then map.
            sorted_expr = prev_expr
            for sk in reversed(se.keys):
                key_var = f"_sk{ctx.state.next_id()}"
                ctx.state.push_scope()
                ctx.state.add_local_var_with_alias(cb.var_name, key_var)
                key_expr = accept(sk.key, t, ctx.with_ctx(key_var))
                ctx.state.pop_scope()
                sorted_call = f"fn_sort({sorted_expr}, lambda {key_var}: {key_expr})"
                sorted_expr = f"fn_reverse({sorted_call})" if sk.descending else sorted_call

            if from_ + 2 < n and isinstance(steps[from_ + 2], FieldRef):
                cjp2 = ctx.ctx_var if not ctx.parent_vars else ctx.parent_vars[-1]
                inner_ctx2 = ctx.with_ctx(var_name).with_cross_join_parent(cjp2)
                rest_expr2 = _compile_path_steps(t, steps, from_ + 2, cjp2, inner_ctx2)
                return f"map_step({sorted_expr}, lambda {var_name}: {rest_expr2})"
            if from_ + 2 < n:
                popped_parents2 = ctx.parent_vars[:-1] if ctx.parent_vars else ()
                inner_ctx2 = ctx.with_ctx(var_name).with_parents(popped_parents2).with_cross_join_parent(
                    ctx.cross_join_parent
                )
                rest_expr2 = _compile_path_steps(t, steps, from_ + 2, var_name, inner_ctx2)
                return f"map_step({sorted_expr}, lambda {var_name}: {rest_expr2})"
            return f"unwrap({sorted_expr})"

        # Regular @$var binding (no cross-join FieldRef follows).
        ni = from_ + 1
        pred_step: PredicateExpr | None = None
        if isinstance((cand := _step_at(steps, ni)), PredicateExpr) and isinstance(cand.source, ContextRef):
            pred_step = cand
            ni += 1
        if ni == n - 1 and isinstance((gb := _step_at(steps, ni)), GroupByExpr) and isinstance(gb.source, ContextRef):
            filtered_expr = prev_expr
            if pred_step is not None:
                pred_expr = accept(pred_step.predicate, t, ctx.with_ctx(var_name))
                filtered_expr = f"dynamic_filter({prev_expr}, lambda {var_name}: {pred_expr})"
            gb_ctx = ctx.with_ctx(filtered_expr).with_primary_context_var(var_name)
            return accept(steps[ni], t, gb_ctx)

        popped_parents = ctx.parent_vars[:-1] if ctx.parent_vars else ()
        inner_ctx = ctx.with_ctx(var_name).with_parents(popped_parents).with_cross_join_parent(ctx.cross_join_parent)
        rest_expr = _compile_path_steps(t, steps, from_ + 1, var_name, inner_ctx)
        return f"map_step({prev_expr}, lambda {var_name}: {rest_expr})"
    finally:
        ctx.state.pop_scope()


# =============================================================================
# #$pos positional binding
# =============================================================================


def _compile_position_binding_step(
    t: Translator, steps: list[AstNode], from_: int, prev_expr: str, ctx: GenCtx
) -> str | None:
    step = steps[from_]
    if not isinstance(step, PositionBinding):
        return None
    pb = step
    ctx.state.push_scope()
    ctx.state.add_local_var(pb.var_name)
    try:
        len(steps)
        new_parents = [*ctx.parent_vars, prev_expr]

        # $#$pos[pred][n] pattern: predicate filter must be applied
        # per-element inside each_indexed (it may reference $pos), but a
        # numeric subscript [n] must be applied to the COLLECTED sequence.
        if (
            isinstance((split_as := _step_at(steps, from_ + 1)), ArraySubscript)
            and isinstance(split_as.source, PredicateExpr)
        ):
            pair_var = f"_pair{ctx.state.next_id()}"
            elem_expr = _pair_elem_expr(pair_var)
            idx_expr = _pair_idx_expr(pair_var)
            ctx.state.add_local_var_with_alias(pb.var_name, idx_expr)
            pred_ctx = ctx.with_ctx(elem_expr).with_parents(new_parents)
            inner_rest_expr = accept(split_as.source, t, pred_ctx)
            post_collect_subscript_idx = accept(split_as.index, t, pred_ctx)
            outer_step_start = from_ + 2
            per_element = from_ >= 2 and isinstance(steps[from_ - 1], ContextRef)
            if per_element:
                map_elem = f"_me{ctx.state.next_id()}"
                each_result = (
                    f"map_step({prev_expr}, lambda {map_elem}: "
                    f"each_indexed({map_elem}, lambda {pair_var}: {inner_rest_expr}))"
                )
            else:
                each_result = f"each_indexed({prev_expr}, lambda {pair_var}: {inner_rest_expr})"
            subscript_result = f"subscript({each_result}, {post_collect_subscript_idx})"
            return _compile_path_steps(t, steps, outer_step_start, subscript_result, ctx)

        # #$pos.SortExpr.rest pattern: sort must be global across ALL
        # elements, not per-element. $pos is only meaningful while computing
        # the sort source (scoped via a nested push/pop); after the sort,
        # elements are re-collected as (item, $pos) tuples and $pos reverts
        # to an ordinary (as-yet-unbound) local, exactly matching Java's
        # design where the ObjectConstructor step downstream re-establishes
        # it (see apply_step's tuple_pos handling).
        if isinstance((se := _step_at(steps, from_ + 1)), SortExpr):
            pair_var2 = f"_pair{ctx.state.next_id()}"
            elem_expr2 = _pair_elem_expr(pair_var2)
            idx_expr2 = _pair_idx_expr(pair_var2)
            ctx.state.push_scope()
            ctx.state.add_local_var_with_alias(pb.var_name, idx_expr2)
            try:
                sort_src_expr = accept(se.source, t, ctx.with_ctx(elem_expr2).with_parents(new_parents))
            finally:
                ctx.state.pop_scope()
            result = f"collect_pos_tuples({prev_expr}, lambda {pair_var2}: {sort_src_expr})"
            for sk in reversed(se.keys):
                tk_var = f"_tk{ctx.state.next_id()}"
                key_expr = accept(sk.key, t, ctx.with_ctx(f"{tk_var}[0]"))
                sorted_call = f"fn_sort({result}, lambda {tk_var}: {key_expr})"
                result = f"fn_reverse({sorted_call})" if sk.descending else sorted_call
            return _compile_path_steps(t, steps, from_ + 2, result, ctx.with_tuple_pos(pb.var_name))

        # When #$pos follows a PredicateExpr inside outer loops
        # (cross-join context), the position must be global across all
        # outer iterations. Use a counter box declared once at the top of
        # _evaluate and incremented via the pure-expression next_counter().
        if from_ > 0 and isinstance(steps[from_ - 1], PredicateExpr) and _has_outer_bindings(steps, from_ - 1):
            ctr_var = f"_ctr{ctx.state.next_id()}"
            ctx.state.local_declarations.append(f"{ctr_var} = [0]")
            map_elem = f"_me{ctx.state.next_id()}"
            ctx.state.add_local_var_with_alias(pb.var_name, f"next_counter({ctr_var})")
            inner_ctx_ctr = ctx.with_ctx(map_elem).with_parents(new_parents)
            rest_expr_ctr = _compile_path_steps(t, steps, from_ + 1, map_elem, inner_ctx_ctr)
            return f"map_step({prev_expr}, lambda {map_elem}: {rest_expr_ctr})"

        pair_var = f"_pair{ctx.state.next_id()}"
        elem_expr = _pair_elem_expr(pair_var)
        idx_expr = _pair_idx_expr(pair_var)
        ctx.state.add_local_var_with_alias(pb.var_name, idx_expr)
        inner_ctx = ctx.with_ctx(elem_expr).with_parents(new_parents)
        rest_expr = _compile_path_steps(t, steps, from_ + 1, elem_expr, inner_ctx)
        # $.$#$pos pattern: preceded by a ContextRef -- apply each_indexed
        # per-element so each gets its own position starting at 0.
        per_element = from_ >= 2 and isinstance(steps[from_ - 1], ContextRef)
        if per_element:
            map_elem = f"_me{ctx.state.next_id()}"
            return f"map_step({prev_expr}, lambda {map_elem}: each_indexed({map_elem}, lambda {pair_var}: {rest_expr}))"
        return f"each_indexed({prev_expr}, lambda {pair_var}: {rest_expr})"
    finally:
        ctx.state.pop_scope()
