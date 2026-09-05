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
    WildcardStep,
    accept,
    array_ctor_always_yields_value,
    consarray_path_head,
    consarray_sub_path,
    staged_consarray_head,
)
from ..parser.parser import BUILTIN_NAMES
from . import scan_fusion, tuple_path_codegen
from .gen_state import emit_callback
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

    if tuple_path_codegen.path_needs_tuples(steps):
        # `@$v`/`#$v` need the reference's tuple stream: a binding step emits
        # one output per (input item x step result) carrying both the binding
        # and the *unchanged* context, which mapping over a collected sequence
        # of bare values cannot express. See runtime/tuples.py.
        keep = any(step_carries_live_mark(s) for s in steps)
        return tuple_path_codegen.visit_tuple_path(t, PathExpr(steps), ctx, keep)

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

    if force_arr and path_ends_with_constructor_step(first_step):
        # `a.[1][].$` -- the `[]` wraps a path whose value is a constructor's
        # own array, so the promotion belongs inside the step helper, exactly
        # as visit_force_array arranges it for the un-stepped `a.[1][]`.
        # Wrapping the finished result instead would see a plain list and
        # pass it through.
        ctx = ctx.with_array_constructor_preserve()
        force_arr = False

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

    if len(steps) > 1 and staged_consarray_head(first_step) is not None:
        head_var = f"_sh{ctx.state.next_id()}"
        rest = _compile_path_steps(t, steps, start_from, head_var, ctx)
        result = f"staged_consarray_head({expr}, lambda {head_var}: {rest})"
        return f"force_array({result})" if force_arr else result

    head_ctor = consarray_path_head(first_step) if len(steps) > 1 else None
    if head_ctor is not None and not head_ctor.elements:
        # Statically empty: the remaining steps are unreachable at run time,
        # so `[].x` folds to the constant `[]`. They are still *compiled*,
        # and the result thrown away, so that a step which cannot be
        # translated at all still reports it -- `[].%` is S0217 in the
        # reference too, which resolves `%` ancestry while building the AST
        # rather than while evaluating.
        _compile_path_steps(t, steps, start_from, "None", ctx)
        result = "array_of()"
    elif head_ctor is not None and not array_ctor_always_yields_value(head_ctor):
        # Might be empty at run time (`[nope].x`, `[nums[false]].x`) --
        # guard it, passing the remaining steps as a thunk so they are
        # skipped rather than evaluated to nothing.
        head_var = f"_ch{ctx.state.next_id()}"
        rest = _compile_path_steps(t, steps, start_from, head_var, ctx)
        result = f"consarray_head({expr}, lambda {head_var}: {rest})"
    else:
        # No constructor head, or one that provably yields a value: the
        # guard could never fire, so emit the ordinary step chain unchanged.
        result = _compile_path_steps(t, steps, start_from, expr, ctx)
    if _path_ends_with_group_by_after_binding(steps):
        result = f"merge_group_by_objects({result})"
    if not force_arr and any(step_carries_live_mark(s) for s in steps):
        # The reference flags the *path* when any of its steps carried a
        # `[]` and reads the flag once at the end, where it suppresses the
        # length-1 collapse: `nums[][0].$` is `[1]`, because the mark
        # survives the steps that follow it. Re-wrapping the already
        # collapsed value is the same thing -- the length-0 collapse is not
        # guarded by the mark, and has already yielded MISSING.
        result = f"force_array({result})"
    if not force_arr:
        return result
    # A consarray-headed path can come out as the head's own cons array, and
    # a `[]` promotes that rather than passing it through: `[][].$` is `[[]]`.
    return f"force_array_cons({result})" if head_ctor is not None else f"force_array({result})"


def step_carries_live_mark(step: AstNode) -> bool:
    """True if a `[]` written on this step can still promote the path."""
    if isinstance(step, ForceArray):
        return step.on_sequence or step_carries_live_mark(step.source)
    if isinstance(step, (PredicateExpr, ArraySubscript, SortExpr)):
        return step_carries_live_mark(step.source)
    return False


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

    # A constructor step already collapses its own result, so a `$` following
    # one must not collapse it a second time -- `one.[$].$` is `[{"x":1}]`.
    # Only a constructor *step* does this; a constructor at position 0 is the
    # path head, compiled as a plain value.
    prev = steps[from_ - 1] if from_ >= 1 else None
    prev_finalised = (
        # A constructor at index 0 is the path *head*, compiled as a plain
        # value, so it has finalised nothing. Anywhere else it is a step and
        # has. A `[]` on a step whose result is a *sequence* does not count:
        # that mark is read once at the end of the path
        # (keepSingletonArray), not at the step, so the step after it still
        # runs normally and `nested[].$` spreads. A `[]` on a constructor
        # step is different -- there the promotion happened at the step, and
        # `a.[1][].$` is `[[1]]`.
        (
            isinstance(prev, ForceArray)
            and (not prev.on_sequence or path_ends_with_constructor_step(prev.source))
        )
        or (from_ >= 2 and isinstance(prev, (ArrayConstructor, ObjectConstructor)))
        or (from_ >= 2 and prev is not None and consarray_sub_path(prev) is not None)
    )
    new_expr = apply_step(
        t, prev_expr, step, ctx, is_last=from_ == len(steps) - 1, prev_is_finalised=prev_finalised
    )
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
    """True if the path's value is an array constructor's own array.

    Trailing `$` steps are transparent: `$` rebinds the context to itself, so
    the constructor's array is still what comes out and it is still `cons`.
    `a.[1].$[]` is `[[1]]`, the same as `a.[1][]`.
    """
    if not isinstance(node, PathExpr) or not node.steps:
        return False
    steps = node.steps
    i = len(steps) - 1
    while i > 0 and isinstance(steps[i], ContextRef):
        i -= 1
    return i > 0 and (isinstance(steps[i], ArrayConstructor) or consarray_sub_path(steps[i]) is not None)


def path_headed_by_consarray(node: AstNode) -> bool:
    """True if the path's head is a flagged array constructor.

    Such a path can come out as the head's own `cons` array -- the §1
    short-circuit returns it whole and the remaining steps never run -- and
    a cons array is not a sequence, so the collapse does not apply to it and
    a `[]` mark promotes it rather than passing it through. `[].x[]` is
    `[[]]` and `[].x^($)` is `[]`, both for the same reason `a.[1][]` is
    `[[1]]`.
    """
    if not isinstance(node, PathExpr) or not node.steps:
        return False
    return consarray_path_head(node.steps[0]) is not None


def path_ends_with_constructor_step(node: AstNode) -> bool:
    """The narrower case: the path's last meaningful step is a *bare* array
    constructor, so the whole result is statically a constructor value and
    the promotion can be folded into the step helper."""
    if not isinstance(node, PathExpr) or not node.steps:
        return False
    steps = node.steps
    i = len(steps) - 1
    while i > 0 and isinstance(steps[i], ContextRef):
        i -= 1
    return i > 0 and isinstance(steps[i], ArrayConstructor)


def _stage_over_object_constructor(step: AstNode) -> bool:
    """True for an object constructor carrying a stage, as one path step."""
    if not isinstance(step, (PredicateExpr, ArraySubscript, ForceArray, SortExpr)):
        return False
    inner = step.source
    while isinstance(inner, (PredicateExpr, ArraySubscript, ForceArray, SortExpr)):
        inner = inner.source
    return isinstance(inner, ObjectConstructor)


def _step_expr(t: Translator, step: AstNode, ctx: GenCtx) -> str:
    """Generates an expression for the FIRST step in a path (uses
    ctx.ctx_var)."""
    if isinstance(step, FieldRef):
        return f"field({ctx.ctx_var}, {py_string(step.name)})"
    if isinstance(step, WildcardStep):
        # A leading `*` enumerates the context node itself only while that
        # node is the evaluation's top-level input, which the reference
        # alone marks as the outer wrapper. Below it the path context is an
        # ordinary element and the step applies per element.
        return f"wildcard_context({ctx.ctx_var})" if ctx.at_input_root else f"wildcard({ctx.ctx_var})"
    if isinstance(step, DescendantStep):
        return f"descendant({ctx.ctx_var})"
    if isinstance(step, ContextRef):
        return ctx.ctx_var
    if isinstance(step, RootRef):
        return ctx.root_var
    return accept(step, t, ctx)


def apply_step(
    t: Translator,
    prev_expr: str,
    step: AstNode,
    ctx: GenCtx,
    is_last: bool = False,
    prev_is_finalised: bool = False,
) -> str:
    """Generates an expression that applies step to prev_expr (steps 2+ in
    a path)."""
    if isinstance(step, FieldRef):
        return f"field({prev_expr}, {py_string(step.name)})"
    if isinstance(step, FunctionCall) and not step.is_variable:
        # `a.g(...)`: the callee is a FIELD of the step context, not a
        # variable and not a built-in, so it resolves per element and
        # maps over a sequence like any other step ($o.g() over a
        # 2-element $o yields two results).
        elem = f"_fc{ctx.state.next_id()}"
        inner = ctx.with_ctx(elem)
        args = [accept(a, t, inner) for a in step.args]
        packed = "None" if not args else (args[0] if len(args) == 1 else f"pack_args({', '.join(args)})")
        callee = f"field_function({elem}, {py_string(step.name)}, {step.name in BUILTIN_NAMES})"
        return f"call_field_step({prev_expr}, lambda {elem}: fn_apply({callee}, {packed}))"
    if isinstance(step, WildcardStep):
        return f"wildcard({prev_expr})"
    if isinstance(step, DescendantStep):
        return f"descendant({prev_expr})"
    if isinstance(step, ContextRef):
        # `$` rebinds the context to itself, so the mapping is an identity --
        # but it is still a *step*, and the reference finalises the sequence
        # it produces: an element that is a plain array gets spread, so
        # `nested.$` over `[[1,2],[3]]` is `[1,2,3]`. `[1].$` is `1`, not
        # `[1]`; `one.[].$` is `[]`, because a constructor's own array is
        # already finalised and passes through verbatim.
        if prev_is_finalised:
            return prev_expr
        return f"context_step({prev_expr}, {is_last!r})"
    if isinstance(step, RootRef):
        return ctx.root_var
    if isinstance(step, PredicateExpr):
        if isinstance(step.predicate, RangeExpr):
            from_expr = accept(step.predicate.from_, t, ctx)
            to_expr = accept(step.predicate.to, t, ctx)
            return f"range_subscript({prev_expr}, {from_expr}, {to_expr})"
        pred = step.predicate
        if not isinstance(step.source, ContextRef):
            # The step carries its own source (`objs.t[[0]]` folds the
            # predicate onto `t`), so navigate it per element and filter
            # *that* -- the same shape the subscript branch below uses. The
            # ContextRef case is the fold-onto-position form, where the
            # incoming stream already is the thing to filter.
            src_node = step.source

            def build_pred(v: str) -> str:
                inner = ctx.with_ctx(v)
                src = accept(src_node, t, inner)
                inner_cb = emit_callback(pred, ctx, "_el", lambda w: accept(pred, t, inner.with_ctx(w)))
                return f"dynamic_filter({src}, {inner_cb})"

            return f"map_step({prev_expr}, {emit_callback(step, ctx, '_c', build_pred)})"
        cb = emit_callback(pred, ctx, "_el", lambda v: accept(pred, t, ctx.with_ctx(v)))
        return f"dynamic_filter({prev_expr}, {cb})"
    if _stage_over_object_constructor(step):
        # `nested.{"k":$}[0]`: the parser folds the stage onto the
        # constructor step, so the step is no longer an ObjectConstructor and
        # the group fold above was lost. The fold belongs to the constructor
        # whatever is wrapped around it.
        cb = emit_callback(step, ctx, "_c", lambda v: accept(step, t, ctx.with_ctx(v)))
        return f"unwrap(map_group_step({prev_expr}, {cb}))"
    if isinstance(step, ArraySubscript):
        sub: ArraySubscript = step

        def build_subscript(v: str) -> str:
            inner = ctx.with_ctx(v)
            return f"subscript({accept(sub.source, t, inner)}, {accept(sub.index, t, inner)})"

        return f"map_step({prev_expr}, {emit_callback(sub, ctx, '_c', build_subscript)})"
    if isinstance(step, ArrayConstructor):
        arr: ArrayConstructor = step
        cb = emit_callback(
            arr, ctx, "_c", lambda v: accept(arr, t, ctx.with_ctx(v).with_in_array_constructor_step())
        )
        # A constructor step's result is a `cons` value: never flattened, and
        # collapsed only across a *sequence* of them. `unwrap` conflated the
        # two, so a non-sequence context lost the array (`a.[1]` became `1`).
        # Under a `[]` wrapper the cons value is promoted instead of passed
        # through -- see constructor_step_keep_singleton.
        if ctx.array_constructor_preserve:
            return f"constructor_step_keep_singleton({prev_expr}, {cb})"
        return f"constructor_step_final({prev_expr}, {cb})"
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
            return f"unwrap(map_group_step({prev_expr}, lambda {tmp_tuple}: {step_expr}))"
        obj: ObjectConstructor = step
        cb = emit_callback(obj, ctx, "_c", lambda v: accept(obj, t, ctx.with_ctx(v)))
        return f"unwrap(map_group_step({prev_expr}, {cb}))"
    if isinstance(step, PathExpr) and consarray_sub_path(step) is not None:
        # A sub-path led by a possibly-empty array constructor, which the
        # optimizer deliberately left unflattened. When its head
        # short-circuits it yields a constructor value, not a sequence, so
        # the parent steps over it without flattening -- see
        # map_consarray_step.
        subpath: PathExpr = step
        cb = emit_callback(subpath, ctx, "_c", lambda v: visit_path_expr(t, subpath, ctx.with_ctx(v)))
        return f"map_consarray_step({prev_expr}, {cb})"
    if isinstance(step, GroupByExpr) and isinstance(step.source, ContextRef):
        # GroupByExpr(ContextRef) appears as a path step only after Rule D's
        # binding rewrite. It must receive the whole accumulated sequence,
        # not be applied per-element.
        return accept(step, t, ctx.with_ctx(prev_expr))
    # Default: rebind the context to prev_expr inside a mapped lambda.
    # A `%` inside the step with no parent available yet makes the *outer*
    # context the parent -- an evaluator-scope local, so such a step never
    # hoists (ParentStep is off the whitelist, which is what enforces it).
    outer_parent = ctx.ctx_var if (ctx.state.contains_parent_step(step) and not ctx.parent_vars) else None
    tail: AstNode = step

    def build_step(v: str) -> str:
        inner = ctx.with_ctx(v)
        if outer_parent is not None:
            inner = inner.with_parents([outer_parent])
        return accept(tail, t, inner)

    mapper = "step_final" if is_last else "map_step"
    return f"{mapper}({prev_expr}, {emit_callback(tail, ctx, '_c', build_step)})"


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
    # A filter the block's scan-fusion pass absorbed reads its slot instead.
    fused_slot = ctx.state.scan_slot(n)
    if fused_slot is not None:
        return fused_slot
    source_node = n.source
    force_arr = isinstance(source_node, ForceArray)
    if isinstance(source_node, ForceArray):
        source_node = source_node.source

    if isinstance(source_node, PathExpr) and source_node.steps:
        last_step = source_node.steps[-1]
        if isinstance(last_step, (PositionBinding, ContextBinding)):
            new_steps = [*source_node.steps, PredicateExpr(ContextRef(), n.predicate)]
            new_source: AstNode = PathExpr(new_steps)
            wrapped: AstNode = ForceArray(new_source, True) if force_arr else new_source
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

    pred = n.predicate
    literal = scan_fusion.field_eq_literal(pred)
    if literal is not None:
        # `$seq[field = <literal>]` -- the value-producing twin of
        # $count(seq[field = literal]), monomorphized in the runtime rather
        # than reached through a per-element callback.
        name, lit_source, _ = literal
        r = f"filter_field_eq({accept(source_node, t, ctx)}, {py_string(name)}, {lit_source})"
        return f"force_array({r})" if force_arr else r

    src_expr = accept(source_node, t, ctx)
    cb = emit_callback(pred, ctx, "_el", lambda v: accept(pred, t, ctx.with_ctx(v)))
    filter_fn = "filter_" if _is_static_boolean_predicate(pred) else "dynamic_filter"
    r = f"{filter_fn}({src_expr}, {cb})"
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
