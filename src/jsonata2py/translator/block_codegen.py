"""Code generation for block expressions and variable bindings.

Ported from org.json_kula.jsonata_jvm.translator.BlockCodeGen.

Python simplification vs. Java: Python has no variable-declaration syntax
(no `JsonNode $x = ...` vs. `$x = ...` distinction), so the Java
`declared: Set<String>` bookkeeping used only to decide which form to emit
is dropped entirely -- every binding is just `v_name = expr`, first
occurrence or not.

Python simplification #2: Java's `discard(expr)` exists because Java
rejects a bare constant/variable-reference as a statement. Python accepts
any expression as a statement, so intermediate block expressions are
emitted as bare statement lines with no wrapper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..parser.ast_nodes import AstNode, Block, Lambda, VariableBinding, accept
from . import scope_analyzer
from .naming import pyvar, pyvar_ref

if TYPE_CHECKING:
    from .gen_ctx import GenCtx
    from .translator import Translator


def visit_block(t: Translator, node: Block, ctx: GenCtx) -> str:
    exprs = node.expressions

    if not exprs:
        return "MISSING"

    # Single expression: skip the helper-def overhead, unless it's a
    # self-referential VariableBinding that needs the holder-array pattern.
    if len(exprs) == 1:
        only = exprs[0]
        needs_holder = (
            isinstance(only, VariableBinding)
            and isinstance(only.value, Lambda)
            and scope_analyzer.contains_var_ref(only.value.body, only.name)
        )
        if not needs_holder:
            return accept(only, t, ctx)

    id_ = ctx.state.next_id()
    method_name = f"_block{id_}"

    block_local_names: set[str] = set()
    for expr in exprs:
        _collect_binding_names(expr, block_local_names)

    # Pre-pass: every variable needing an array-holder (self-recursive
    # lambdas AND forward references), pre-declared at the top of the
    # helper so lambdas defined earlier can safely capture them.
    holder_needed = scope_analyzer.compute_holder_needed(exprs, block_local_names)

    # Outer-scope locals that are free variables in this block's body --
    # passed as extra parameters since a Python function can't see an
    # enclosing generated function's locals across a `def` boundary the
    # way this translator structures block methods (each is called, not
    # nested).
    captured_vars = scope_analyzer.collect_free_outer_vars(exprs, block_local_names, ctx.state)

    extra_param_decls: list[str] = []
    extra_call_args: list[str] = []
    # Names whose current alias is a computed *expression* (a #$pos
    # pair-unpack) rather than a bound identifier can't be forwarded
    # as-is -- the expression is only valid where it was defined. Evaluate
    # it once here (call arg) and rebind the name to a fresh plain
    # parameter (applied below, before compiling the block body) so the
    # body's compiled text references the new parameter instead.
    rebind_overrides: list[tuple[str, str]] = []
    for v in captured_vars:
        if v in ctx.state.holder_vars:
            extra_param_decls.append(pyvar_ref(v))
            extra_call_args.append(pyvar_ref(v))
            continue
        alias = ctx.state.get_alias(v)
        if alias is not None and not alias.isidentifier():
            fresh_name = f"_cap{ctx.state.next_id()}"
            extra_param_decls.append(fresh_name)
            extra_call_args.append(alias)
            rebind_overrides.append((v, fresh_name))
            continue
        py_name = alias if alias is not None else pyvar(v)
        extra_param_decls.append(py_name)
        extra_call_args.append(py_name)

    # If any expression in the block references %, pass the outer
    # parent_vars as extra parameters so % resolves correctly inside.
    has_parent_ref = any(ctx.state.contains_parent_step(e) for e in exprs)
    outer_parent_vars = list(ctx.parent_vars)
    parent_param_names: list[str] = []
    parent_param_decls: list[str] = []
    parent_call_args: list[str] = []
    if has_parent_ref and outer_parent_vars:
        for pi, pv in enumerate(outer_parent_vars):
            pn = f"_pp{id_}_{pi}"
            parent_param_names.append(pn)
            parent_param_decls.append(pn)
            parent_call_args.append(pv)

    from .gen_ctx import GenCtx  # local import to avoid a module cycle

    inner_ctx = GenCtx("_ctx", "_root", ctx.state).with_parents(parent_param_names)

    # Activate all holder variables BEFORE compiling the body so
    # visit_variable_ref emits v_name_ref[0] for every holder reference,
    # including forward references to variables not yet assigned.
    ctx.state.holder_vars |= holder_needed
    ctx.state.push_scope()
    for name in holder_needed:
        ctx.state.add_local_var(name)
    for name, fresh in rebind_overrides:
        ctx.state.add_local_var_with_alias(name, fresh)

    try:
        body_lines: list[str] = []
        for name in holder_needed:
            body_lines.append(f"{pyvar_ref(name)} = [MISSING]")

        for expr in exprs[:-1]:
            if isinstance(expr, VariableBinding):
                emit_var_binding(t, expr, body_lines, inner_ctx)
            else:
                body_lines.append(accept(expr, t, inner_ctx))

        # The last expression is in tail position when the block itself is
        # in tail position (i.e. it is the body of a lambda).
        last_ctx = inner_ctx.with_tail_position(True) if ctx.is_tail_position else inner_ctx
        last = exprs[-1]
        if isinstance(last, VariableBinding):
            emit_var_binding(t, last, body_lines, last_ctx)
            body_lines.append(f"return {pyvar(last.name)}")
        else:
            body_lines.append(f"return {accept(last, t, last_ctx)}")

        params = ["_root", "_ctx", *extra_param_decls, *parent_param_decls]
        def_lines = [f"def {method_name}({', '.join(params)}):"]
        def_lines.extend(f"    {line}" for line in body_lines)
        ctx.state.helper_defs.append("\n".join(def_lines) + "\n")
    finally:
        ctx.state.pop_scope()
        ctx.state.holder_vars -= holder_needed

    call_args = ", ".join([ctx.root_var, ctx.ctx_var, *extra_call_args, *parent_call_args])
    return f"{method_name}({call_args})"


def emit_var_binding(t: Translator, vb: VariableBinding, body_lines: list[str], ctx: GenCtx) -> None:
    """Emits a single VariableBinding as a `v_name = ...` statement. If
    name is in ctx.state.holder_vars the holder array was already
    pre-declared at the top of the block; only the assignment and the
    corresponding v_name_ref[0] = v_name update are needed."""
    from ..parser.ast_nodes import ParentStep

    # Special case: desugared function call with % as callee and no parent
    # context. Emitting MISSING lets fn_apply raise T1006 ("not a
    # function") at runtime rather than S0217 during translation.
    if vb.name.startswith("__call_") and isinstance(vb.value, ParentStep) and not ctx.parent_vars:
        body_lines.append(f"{pyvar(vb.name)} = MISSING")
        return

    # Chained assignment $a := $b := val: emit the inner binding first,
    # then use the inner variable as the value of the outer binding.
    if isinstance(vb.value, VariableBinding):
        emit_var_binding(t, vb.value, body_lines, ctx)
        inner_ref = pyvar(vb.value.name)
        body_lines.append(f"{pyvar(vb.name)} = {inner_ref}")
        if vb.name in ctx.state.holder_vars:
            body_lines.append(f"{pyvar_ref(vb.name)}[0] = {pyvar(vb.name)}")
        ctx.state.add_local_var(vb.name)
        return

    val_expr = accept(vb.value, t, ctx)
    body_lines.append(f"{pyvar(vb.name)} = {val_expr}")
    if vb.name in ctx.state.holder_vars:
        body_lines.append(f"{pyvar_ref(vb.name)}[0] = {pyvar(vb.name)}")
    # Lazily register in scope so subsequent statements resolve to this
    # local (and so THIS binding's own RHS -- already generated above --
    # saw the outer alias, not this not-yet-registered inner local).
    ctx.state.add_local_var(vb.name)


def _collect_binding_names(expr: AstNode, names: set[str]) -> None:
    """Recursively collects variable names from a VariableBinding chain
    ($a := $b := 5)."""
    if isinstance(expr, VariableBinding):
        names.add(expr.name)
        _collect_binding_names(expr.value, names)
