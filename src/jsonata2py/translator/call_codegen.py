"""Code generation for function calls and lambda expressions.

Ported from org.json_kula.jsonata_jvm.translator.FunctionCallCodeGen.

Structural divergence from Java (documented, not accidental): Java can
embed an anonymous *block-bodied* lambda `(packed -> { stmt; stmt; return
expr; })` at any expression position, and such a lambda closes over
enclosing Java locals. Python lambdas are expression-only -- they cannot
contain statements -- so any callback needing more than one statement
(every multi-parameter unpack, and any signature-checked lambda) is
compiled to a named top-level `def` instead.

For 0- and 1-parameter callbacks this changes nothing: the body is always
a single expression (Block/VariableBinding route through block_codegen's
own helper-def mechanism and yield a call expression), so a real Python
`lambda` closes over the enclosing generated function's locals exactly
like Java's anonymous lambda does.

For 2+ parameter callbacks, Java's `buildInlineLambda` uses a *closing*
anonymous lambda -- it can see outer block-locals directly, including its
own holder-ref for self-recursion (a 2+-param lambda binding itself
recursively, e.g. `$gcd := function($a,$b){...$gcd(...)...}`, is an
ordinary and common pattern, not an edge case). A named top-level `def`
has no such access, so this port threads every referenced outer-scope
name through as an *explicit extra parameter* -- exactly the mechanism
block_codegen.py already uses for its own helper defs. The def itself
still can't close over anything, but the *wrapper* returned to the call
site (`lambda _pk: _methodN(_root, _pk, <extra args>)`) is a genuine
single-expression Python lambda embedded right where the outer names
are still in scope, so it supplies their current values as ordinary
arguments. Net effect: full closure-equivalent behaviour, achieved by
passing values instead of relying on lexical capture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..parser.ast_nodes import (
    AstNode,
    BinaryOp,
    Block,
    ContextRef,
    FieldRef,
    FunctionCall,
    Lambda,
    LambdaCall,
    PathExpr,
    VariableBinding,
    VariableRef,
    accept,
)
from . import scope_analyzer
from .naming import py_string, pyvar, pyvar_ref

if TYPE_CHECKING:
    from .gen_ctx import GenCtx
    from .translator import Translator


def _captured_outer_context(
    ctx: GenCtx, body: AstNode, own_params: list[str]
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Returns (extra_param_decls, extra_call_args, rebind_overrides) for
    outer-scope free variables body references beyond its own_params --
    including the lambda's own holder-ref, if body is self-recursive. Must
    be called BEFORE pushing a new scope for own_params, so
    ctx.state.scope_stack reflects only the enclosing (outer) scopes.

    rebind_overrides is a list of (jsonata_name, fresh_python_name) pairs
    the CALLER must apply via add_local_var_with_alias -- inside the SAME
    scope it pushes for own_params, before compiling body -- for any
    captured name whose current alias is a computed *expression* (e.g. a
    #$pos pair-unpack) rather than a bound identifier. Such an expression
    is only valid at the call site (it references a variable local to the
    enclosing lambda, e.g. a tuple parameter); it's evaluated once here as
    the extra call arg, and the body must be recompiled to reference a
    fresh plain parameter instead, since the generated def it ends up in
    has no closure over the original expression's variables.
    """
    outer_locals: set[str] = set()
    for scope in ctx.state.scope_stack:
        outer_locals |= scope
    if not outer_locals:
        return [], [], []
    used: dict[str, None] = {}
    scope_analyzer.collect_free_vars_into(body, used, set(own_params))
    captured_vars = [name for name in used if name in outer_locals]

    extra_param_decls: list[str] = []
    extra_call_args: list[str] = []
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
    return extra_param_decls, extra_call_args, rebind_overrides


def gen_user_function_call(t: Translator, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
    """Generates a call to a user-defined variable function: $myFn(args)."""
    array_literal = "[]" if not args else f"[{', '.join(args)}]"
    if ctx.state.is_local(n.name):
        if n.name in ctx.state.holder_vars:
            fn_ref = f"{pyvar_ref(n.name)}[0]"
        else:
            alias = ctx.state.get_alias(n.name)
            fn_ref = alias if alias is not None else pyvar(n.name)
        # fn_apply_tco enables TCO (trampoline) when in tail position.
        apply_fn = "fn_apply_tco" if ctx.is_tail_position else "fn_apply"
        if len(args) <= 1:
            arg = "None" if not args else args[0]
            return f"{apply_fn}({fn_ref}, {arg})"
        return f"{apply_fn}({fn_ref}, pack_args({', '.join(args)}))"
    return f"call_bound_function({py_string(n.name)}, {array_literal})"


def _ordering_key_path(body: AstNode, params: list[str]) -> tuple[AstNode, bool] | None:
    """Recognises a comparator that is nothing more than an ordering on
    one key: function($a, $b) { $a.K > $b.K } and its three mirrors.

    Returns (key_expression_rooted_at_a_parameter, descending), or None
    when the body is anything else.

    Deliberately narrow. The two sides must be the same plain field path
    -- no predicates, no parent steps, no calls -- differing only in
    which parameter they start from, because that is what makes
    "evaluate the key once per element" identical to "evaluate the body
    once per comparison".
    """
    if not isinstance(body, BinaryOp) or body.op not in (">", "<"):
        return None
    left_root, left_steps = _param_rooted_path(body.left, params)
    right_root, right_steps = _param_rooted_path(body.right, params)
    if left_root is None or right_root is None or left_root == right_root:
        return None
    if len(left_steps) != len(right_steps):
        return None
    for a, b in zip(left_steps, right_steps, strict=True):
        if not isinstance(a, FieldRef) or not isinstance(b, FieldRef) or a.name != b.name:
            return None
    # `$a.K > $b.K` puts a *after* b when a.K is greater, i.e. ascending.
    # Rooting the left side at the second parameter mirrors that.
    ascending = (body.op == ">") == (left_root == params[0])
    key = PathExpr([ContextRef(), *left_steps]) if left_steps else ContextRef()
    return key, not ascending


def _param_rooted_path(node: AstNode, params: list[str]) -> tuple[str | None, list[AstNode]]:
    """Splits node into (parameter it is rooted at, the steps after it)."""
    if isinstance(node, VariableRef) and node.name in params:
        return node.name, []
    if isinstance(node, PathExpr) and node.steps:
        head = node.steps[0]
        if isinstance(head, VariableRef) and head.name in params:
            return head.name, list(node.steps[1:])
    return None, []


def gen_sort(t: Translator, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
    """A 2-param Lambda is a comparator (fn_sort_comparator); a 1-param
    Lambda is a key extractor (fn_sort)."""
    if len(args) == 1:
        return f"fn_sort({args[0]})"
    fn_arg = n.args[1]
    if isinstance(fn_arg, Lambda) and len(fn_arg.params) >= 2:
        # A comparator that only orders by one key does not need to be
        # called once per comparison -- see fn_sort_by_ordering_key.
        if len(fn_arg.params) == 2 and not ctx.state.contains_parent_step(fn_arg.body):
            match = _ordering_key_path(fn_arg.body, fn_arg.params)
            if match is not None:
                key_node, descending = match
                key_var = f"_sk{ctx.state.next_id()}"
                key_expr = accept(key_node, t, ctx.with_ctx(key_var).with_tail_position(False))
                return (
                    f"fn_sort_by_ordering_key({args[0]}, lambda {key_var}: {key_expr}, {descending})"
                )
        lambda_expr = gen_unpack_lambda(t, fn_arg, ctx, 2)
        return f"fn_sort_comparator({args[0]}, {lambda_expr})"
    if isinstance(fn_arg, Lambda):
        return gen_higher_order(t, "fn_sort", n, args, ctx, 0, 1)
    # The callback is a value: its arity -- and so comparator-vs-key -- is a runtime question.
    return f"fn_sort({args[0]}, {accept(fn_arg, t, ctx)})"


def gen_higher_order(
    t: Translator, rt_method: str, n: FunctionCall, args: list[str], ctx: GenCtx, seq_arg_index: int, fn_arg_index: int
) -> str:
    """Generates a call to a higher-order function (fn_map, fn_filter, ...)
    where the argument at fn_arg_index may be a Lambda node."""
    seq_expr = args[seq_arg_index]
    fn_arg = n.args[fn_arg_index]

    if isinstance(fn_arg, Lambda) and len(fn_arg.params) > 1 and rt_method in ("fn_map", "fn_filter"):
        indexed_method = "fn_map_indexed" if rt_method == "fn_map" else "fn_filter_indexed"
        lambda_expr = gen_unpack_lambda(t, fn_arg, ctx, 3)
        return f"{indexed_method}({seq_expr}, {lambda_expr})"

    if not isinstance(fn_arg, Lambda):
        # The callback is a value; the runtime overload picks the plain or
        # indexed variant from its arity.
        return f"{rt_method}({seq_expr}, {accept(fn_arg, t, ctx)})"
    lambda_expr = inline_lambda(t, fn_arg, ctx)
    return f"{rt_method}({seq_expr}, {lambda_expr})"


def gen_single(t: Translator, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
    if len(n.args) < 2:
        seq_expr = ctx.ctx_var if not args else args[0]
        return f"fn_single({seq_expr})"
    seq_expr = args[0]
    fn_arg = n.args[1]
    if isinstance(fn_arg, Lambda) and len(fn_arg.params) > 1:
        lambda_expr = gen_unpack_lambda(t, fn_arg, ctx, 3)
        return f"fn_single_indexed({seq_expr}, {lambda_expr})"
    if not isinstance(fn_arg, Lambda):
        return f"fn_single({seq_expr}, {accept(fn_arg, t, ctx)})"
    return f"fn_single({seq_expr}, {inline_lambda(t, fn_arg, ctx)})"


def gen_reduce(t: Translator, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
    """Generates fn_reduce(arr, fn, init). The runtime passes an
    [acc, elem, index, array] tuple to the lambda on each iteration."""
    arr_expr = args[0]
    fn_arg = n.args[1]
    init_expr = args[2] if len(args) > 2 else "MISSING"
    if isinstance(fn_arg, Lambda) and len(fn_arg.params) < 2:
        # D3050: reducer function must accept at least 2 parameters
        return (
            f'fn_throw("D3050", "The second argument of $reduce must accept at least 2 '
            f'parameters, got {len(fn_arg.params)}")'
        )
    if isinstance(fn_arg, Lambda) and len(fn_arg.params) > 1:
        lambda_expr = build_inline_lambda(t, fn_arg, ctx)
    elif isinstance(fn_arg, Lambda):
        lambda_expr = inline_lambda(t, fn_arg, ctx)
    else:
        # fn is an expression that evaluates to a function value (e.g. a
        # variable holding a user-defined function). Wrap it so fn_reduce
        # receives a plain callable.
        fn_code = accept(fn_arg, t, ctx)
        lambda_expr = f"(lambda _elem: fn_apply({fn_code}, _elem))"
    return f"fn_reduce({arr_expr}, {lambda_expr}, {init_expr})"


def gen_each(t: Translator, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
    """Generates fn_each(obj, fn) where the lambda receives a
    [value, key, object] tuple (value first, per JSONata spec)."""
    if len(n.args) == 1:
        obj_expr = ctx.ctx_var
        fn_arg_idx = 0
    else:
        obj_expr = args[0]
        fn_arg_idx = 1
    fn_arg = n.args[fn_arg_idx]
    if not isinstance(fn_arg, Lambda):
        return f"fn_each({obj_expr}, {accept(fn_arg, t, ctx)})"
    lambda_expr = gen_unpack_lambda(t, fn_arg, ctx, 3)
    return f"fn_each({obj_expr}, {lambda_expr})"


def gen_sift(t: Translator, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
    """Generates fn_sift(obj, fn) where the lambda receives a
    [value, key, object] tuple."""
    if len(n.args) == 1:
        obj_expr = ctx.ctx_var
        fn_arg_idx = 0
    else:
        obj_expr = args[0]
        fn_arg_idx = 1
    fn_arg = n.args[fn_arg_idx]
    if not isinstance(fn_arg, Lambda):
        return f"fn_sift({obj_expr}, {accept(fn_arg, t, ctx)})"
    lambda_expr = gen_unpack_lambda(t, fn_arg, ctx, 3)
    return f"fn_sift({obj_expr}, {lambda_expr})"


def gen_unpack_lambda(t: Translator, lam: Lambda, ctx: GenCtx, tuple_len: int) -> str:
    """Generates a named helper def that receives a tuple and unpacks its
    elements into the lambda's named parameters before evaluating the
    body. Outer-scope names the body references (including its own
    holder-ref) are threaded through as explicit extra parameters -- see
    module docstring."""
    id_ = ctx.state.next_id()
    method_name = f"_unpack{id_}"
    extra_param_decls, extra_call_args, rebind_overrides = _captured_outer_context(ctx, lam.body, lam.params)

    ctx.state.push_scope()
    for p in lam.params:
        ctx.state.add_local_var(p)
    for name, fresh in rebind_overrides:
        ctx.state.add_local_var_with_alias(name, fresh)
    try:
        body_lines: list[str] = []
        for i, p in enumerate(lam.params):
            if i < tuple_len:
                body_lines.append(f"{pyvar(p)} = _el[{i}] if isinstance(_el, list) else _el")
            else:
                body_lines.append(f"{pyvar(p)} = MISSING")
        body_expr = accept(lam.body, t, ctx.with_ctx("_el"))
        body_lines.append(f"return {body_expr}")
        def_params = ["_root", "_el", *extra_param_decls]
        def_lines = [f"def {method_name}({', '.join(def_params)}):"]
        def_lines.extend(f"    {line}" for line in body_lines)
        ctx.state.helper_defs.append("\n".join(def_lines) + "\n")
    finally:
        ctx.state.pop_scope()

    call_args = ", ".join([ctx.root_var, "_ua", *extra_call_args])
    return f"(lambda _ua: {method_name}({call_args}))"


def inline_lambda(t: Translator, lam: Lambda, ctx: GenCtx) -> str:
    """Inlines a Lambda as a Python lambda expression, binding its first
    parameter to the element and any remaining parameters to MISSING."""
    if not lam.params:
        body_expr = accept(lam.body, t, ctx)
        return f"(lambda _ignored: {body_expr})"
    if len(lam.params) == 1:
        p1 = pyvar(lam.params[0])
        ctx.state.push_scope()
        ctx.state.add_local_var(lam.params[0])
        try:
            body_expr = accept(lam.body, t, ctx)
            return f"(lambda {p1}: {body_expr})"
        finally:
            ctx.state.pop_scope()
    return gen_lambda_method(t, lam, ctx)


def _wrap_if_self_ref(body: AstNode) -> AstNode:
    """Wraps a self-referential VariableBinding in a synthetic Block so
    block_codegen.visit_block applies the holder-array pattern."""
    if (
        isinstance(body, VariableBinding)
        and isinstance(body.value, Lambda)
        and scope_analyzer.contains_var_ref(body.value.body, body.name)
    ):
        return Block([body])
    return body


def build_inline_lambda(t: Translator, lam: Lambda, ctx: GenCtx) -> str:
    """Lambda bodies are always in tail position -- their last expression
    is the return value."""
    tail_ctx = ctx.with_tail_position(True)
    if not lam.params:
        body = accept(_wrap_if_self_ref(lam.body), t, tail_ctx)
        return f"(lambda _ignored: {body})"
    if len(lam.params) == 1:
        p = pyvar(lam.params[0])
        ctx.state.push_scope()
        ctx.state.add_local_var(lam.params[0])
        try:
            body = accept(_wrap_if_self_ref(lam.body), t, tail_ctx)
            return f"(lambda {p}: {body})"
        finally:
            ctx.state.pop_scope()
    # Multi-param: named def, no outer closure (see module docstring).
    return gen_lambda_method(t, lam, ctx, tail=True)


def build_inline_lambda_with_sig(t: Translator, lam: Lambda, ctx: GenCtx) -> str:
    """Like build_inline_lambda but prepends signature type checks when the
    lambda has a non-None signature."""
    if lam.signature is None:
        return build_inline_lambda(t, lam, ctx)
    param_types = _parse_signature_params(lam.signature, len(lam.params))
    has_checks = any(_build_type_check(pt, "_x", 1) is not None for pt in param_types)
    if not has_checks:
        return build_inline_lambda(t, lam, ctx)

    id_ = ctx.state.next_id()
    method_name = f"_sig{id_}"
    extra_param_decls, extra_call_args, rebind_overrides = _captured_outer_context(ctx, lam.body, lam.params)
    ctx.state.push_scope()
    java_names = []
    for param in lam.params:
        py_name = f"{pyvar(param)}__{id_}"
        java_names.append(py_name)
        ctx.state.add_local_var_with_alias(param, py_name)
    for name, fresh in rebind_overrides:
        ctx.state.add_local_var_with_alias(name, fresh)
    try:
        body_lines: list[str] = []
        if not lam.params:
            pass
        elif len(lam.params) == 1:
            body_lines.append(f"{java_names[0]} = _pk[0] if isinstance(_pk, list) else _pk")
        else:
            body_lines.append(f"{java_names[0]} = _pk[0] if isinstance(_pk, list) else _pk")
            for i in range(1, len(lam.params)):
                body_lines.append(
                    f"{java_names[i]} = _pk[{i}] if isinstance(_pk, list) and len(_pk) > {i} else MISSING"
                )
        for i, pt in enumerate(param_types):
            if i >= len(lam.params):
                break
            check = _build_type_check(pt, java_names[i], i + 1)
            if check:
                body_lines.extend(check.split("\n"))
        body = accept(lam.body, t, ctx.with_tail_position(True))
        body_lines.append(f"return {body}")
        def_params = ["_root", "_pk", *extra_param_decls]
        def_lines = [f"def {method_name}({', '.join(def_params)}):"]
        def_lines.extend(f"    {line}" for line in body_lines)
        ctx.state.helper_defs.append("\n".join(def_lines) + "\n")
    finally:
        ctx.state.pop_scope()
    call_args = ", ".join([ctx.root_var, "_pk", *extra_call_args])
    return f"(lambda _pk: {method_name}({call_args}))"


def gen_lambda_method(t: Translator, lam: Lambda, ctx: GenCtx, tail: bool = False) -> str:
    """Generates a named helper def for a multi-parameter lambda and
    returns a bare-name reference usable as a callable. Always called with
    2+ params (inline_lambda/build_inline_lambda handle 0/1 with a real
    Python lambda instead)."""
    id_ = ctx.state.next_id()
    method_name = f"_lambda{id_}"
    body_for_capture = _wrap_if_self_ref(lam.body) if tail else lam.body
    extra_param_decls, extra_call_args, rebind_overrides = _captured_outer_context(ctx, body_for_capture, lam.params)

    ctx.state.push_scope()
    for p in lam.params:
        ctx.state.add_local_var(p)
    for name, fresh in rebind_overrides:
        ctx.state.add_local_var_with_alias(name, fresh)
    try:
        body_lines: list[str] = []
        body_ctx = ctx.with_ctx("_pk")
        if tail:
            body_ctx = body_ctx.with_tail_position(True)
        body_lines.append(f"{pyvar(lam.params[0])} = _pk[0] if isinstance(_pk, list) else _pk")
        for i in range(1, len(lam.params)):
            body_lines.append(
                f"{pyvar(lam.params[i])} = _pk[{i}] if isinstance(_pk, list) and len(_pk) > {i} "
                f"and _pk[{i}] is not MISSING else MISSING"
            )
        body_expr = accept(body_for_capture, t, body_ctx)
        body_lines.append(f"return {body_expr}")
        def_params = ["_root", "_pk", *extra_param_decls]
        def_lines = [f"def {method_name}({', '.join(def_params)}):"]
        def_lines.extend(f"    {line}" for line in body_lines)
        ctx.state.helper_defs.append("\n".join(def_lines) + "\n")
    finally:
        ctx.state.pop_scope()

    call_args = ", ".join([ctx.root_var, "_pk", *extra_call_args])
    return f"(lambda _pk: {method_name}({call_args}))"


# =============================================================================
# LambdaCall (immediately-invoked lambda with signature)
# =============================================================================


def gen_lambda_call(t: Translator, n: LambdaCall, ctx: GenCtx) -> str:
    """Generates code for an immediately-invoked lambda with a signature:
    type coercion/checking (T0410/T0412) and context binding ('-' flag).

    Focus ('-') params consume positional args when available, falling
    back to context only when fewer explicit args are supplied than
    non-focus params require. Variadic ('+') specs cover multiple
    consecutive param positions.
    """
    lam = n.lambda_
    sig = lam.signature
    call_args = n.args
    params = lam.params
    num_params = len(params)
    num_args = len(call_args)

    all_specs = _parse_signature_params(sig, 2**31 - 1)
    spec_per_param = _compute_spec_per_param(all_specs, num_params)

    is_focus = [spec is not None and (spec == "-" or spec.endswith("-")) for spec in spec_per_param]

    last_variadic_spec = None
    for s in all_specs:
        if s.endswith("+"):
            last_variadic_spec = s

    if num_args > num_params and last_variadic_spec is None:
        return 'fn_throw("T0410", "Too many arguments supplied to lambda expression")'

    arg_exprs = [accept(arg, t, ctx) for arg in call_args]

    final_args: list[str]
    if num_args >= num_params:
        final_args = list(arg_exprs[:num_params])
    else:
        assigned_arg_idx = [-1] * num_params
        queue_idx = 0
        for i in range(num_params):
            if queue_idx >= num_args:
                break
            if not is_focus[i]:
                assigned_arg_idx[i] = queue_idx
                queue_idx += 1
        for i in range(num_params):
            if queue_idx >= num_args:
                break
            if is_focus[i]:
                assigned_arg_idx[i] = queue_idx
                queue_idx += 1
        final_args = []
        for i in range(num_params):
            if assigned_arg_idx[i] >= 0:
                final_args.append(arg_exprs[assigned_arg_idx[i]])
            elif is_focus[i]:
                final_args.append(ctx.ctx_var)
            else:
                final_args.append("MISSING")

    num_extra = max(0, num_args - num_params)

    id_ = ctx.state.next_id()
    method_name = f"_lc{id_}"

    # Invoked immediately at its own definition site (not stored/passed as
    # a value), so captured outer names can just be threaded straight
    # through as extra def params -- no wrapper lambda needed, the call
    # site below is already embedded in the scope where they're visible.
    extra_param_decls, extra_call_args, rebind_overrides = _captured_outer_context(ctx, lam.body, params)

    param_names = [pyvar(p) for p in params]
    extra_names = [f"_extra{e}" for e in range(num_extra)]
    def_params = ["_root", *param_names, *extra_names, *extra_param_decls]

    body_lines: list[str] = []
    for i in range(num_params):
        check = _build_type_check(spec_per_param[i], param_names[i], i + 1)
        if check:
            body_lines.extend(check.split("\n"))
    if num_extra > 0 and last_variadic_spec is not None:
        for e in range(num_extra):
            check = _build_type_check(last_variadic_spec, extra_names[e], num_params + e + 1)
            if check:
                body_lines.extend(check.split("\n"))

    ctx.state.push_scope()
    for p in params:
        ctx.state.add_local_var(p)
    for name, fresh in rebind_overrides:
        ctx.state.add_local_var_with_alias(name, fresh)
    try:
        body_ctx = ctx.with_ctx(ctx.ctx_var if not params else pyvar(params[0]))
        body_expr = accept(lam.body, t, body_ctx)
        body_lines.append(f"return {body_expr}")
    finally:
        ctx.state.pop_scope()

    def_lines = [f"def {method_name}({', '.join(def_params)}):"]
    def_lines.extend(f"    {line}" for line in body_lines)
    ctx.state.helper_defs.append("\n".join(def_lines) + "\n")

    call_arg_list = [ctx.root_var, *final_args, *arg_exprs[num_params:num_args], *extra_call_args]
    return f"{method_name}({', '.join(call_arg_list)})"


def _compute_spec_per_param(all_specs: list[str], num_params: int) -> list[str | None]:
    """Maps each param position to the signature spec that should validate
    it. Variadic ('+') specs expand to cover multiple consecutive
    positions."""
    result: list[str | None] = [None] * num_params
    spec_idx = 0
    param_idx = 0
    while spec_idx < len(all_specs) and param_idx < num_params:
        spec = all_specs[spec_idx]
        variadic = spec.endswith("+")

        remaining_required = sum(1 for k in range(spec_idx + 1, len(all_specs)) if not all_specs[k].endswith("+"))

        result[param_idx] = spec
        param_idx += 1

        if variadic:
            while param_idx < num_params and (num_params - param_idx) > remaining_required:
                result[param_idx] = spec
                param_idx += 1
        spec_idx += 1
    return result


def _parse_signature_params(sig: str | None, param_count: int) -> list[str]:
    """Parses a signature string like <nn:a> or <a<n>> and returns the
    per-parameter type specs. Strips the return type (after ':')."""
    result: list[str] = []
    if sig is None or not sig.startswith("<"):
        return result
    inner = sig[1:-1]
    colon_idx = -1
    depth = 0
    for i, c in enumerate(inner):
        if c in ("<", "("):
            depth += 1
        elif c in (">", ")"):
            depth -= 1
        elif c == ":" and depth == 0:
            colon_idx = i
            break
    if colon_idx >= 0:
        inner = inner[:colon_idx]

    i = 0
    n = len(inner)
    while i < n and len(result) < param_count:
        c = inner[i]
        if c == "(":
            j = i + 1
            d = 1
            while j < n and d > 0:
                if inner[j] == "(":
                    d += 1
                elif inner[j] == ")":
                    d -= 1
                j += 1
            union = inner[i:j]
            if j < n and inner[j] == "<":
                k = j + 1
                d2 = 1
                while k < n and d2 > 0:
                    if inner[k] == "<":
                        d2 += 1
                    elif inner[k] == ">":
                        d2 -= 1
                    k += 1
                union += inner[j:k]
                j = k
            mod = []
            while j < n and inner[j] in "+-?":
                mod.append(inner[j])
                j += 1
            result.append(union + "".join(mod))
            i = j
        elif c in "aobnsljufx":
            spec = [c]
            i += 1
            if i < n and inner[i] == "<":
                j = i + 1
                d = 1
                while j < n and d > 0:
                    if inner[j] == "<":
                        d += 1
                    elif inner[j] == ">":
                        d -= 1
                    j += 1
                spec.append(inner[i:j])
                i = j
            while i < n and inner[i] in "+-?":
                spec.append(inner[i])
                i += 1
            result.append("".join(spec))
        elif c == "-":
            result.append("-")
            i += 1
        else:
            # Unknown type char: skip it (and any trailing angle-bracket
            # type param) as a last resort for unrecognised syntax.
            i += 1
            if i < n and inner[i] == "<":
                j = i + 1
                d = 1
                while j < n and d > 0:
                    if inner[j] == "<":
                        d += 1
                    elif inner[j] == ">":
                        d -= 1
                    j += 1
                i = j
    return result


def _build_type_check(ptype: str | None, param_var: str, arg_num: int) -> str | None:
    """Generates a runtime type-check statement for a parameter. Returns
    None if no check is needed."""
    if not ptype or ptype == "-":
        return None
    import re

    base = re.sub(r"[+?\-]$", "", ptype)
    if not base or base == "-":
        return None
    optional = ptype.endswith("?")
    focus = ptype.endswith("-")
    if optional and base in ("n", "s", "b"):
        return None

    def _wrap(stmt: str) -> str:
        if not focus:
            return stmt
        return f"if {param_var} is not MISSING:\n    {stmt}"

    if base[0] == "f":
        raise_stmt = (
            f'if {param_var} is not MISSING and not is_lambda_token({param_var}): '
            f'fn_throw("T0410", "Argument {arg_num} of function is not a function")'
        )
        return _wrap(raise_stmt)
    if base == "x":
        return None
    if base == "n":
        return _wrap(
            f'if {param_var} is not MISSING and not is_number({param_var}): '
            f'fn_throw("T0410", "Argument {arg_num} of function is not a number")'
        )
    if base == "s":
        return _wrap(
            f'if {param_var} is not MISSING and not isinstance({param_var}, str): '
            f'fn_throw("T0410", "Argument {arg_num} of function is not a string")'
        )
    if base == "b":
        return _wrap(
            f'if {param_var} is not MISSING and not isinstance({param_var}, bool): '
            f'fn_throw("T0410", "Argument {arg_num} of function is not a boolean")'
        )
    if base.startswith("a<"):
        elem_type = base[2:-1]
        elem_check = _get_elem_type_check(elem_type, "_ae")
        coerce = (
            f"if {param_var} is not MISSING and not isinstance({param_var}, list): "
            f"{param_var} = [{param_var}]"
        )
        if elem_check is None:
            return coerce
        # `coerce` and the `if not MISSING:` line are SIBLING statements
        # (both at the caller's base indent, applied uniformly per split
        # physical line -- see call sites' `.split("\n")` handling), so
        # only the lines nested *under* that `if` get relative indent.
        return (
            f"{coerce}\n"
            f"if {param_var} is not MISSING:\n"
            f"    for _ae in {param_var}:\n"
            f'        if {elem_check}: fn_throw("T0412", "Array element is not of expected type")'
        )
    if base == "a":
        return f"if {param_var} is not MISSING and not isinstance({param_var}, list): {param_var} = [{param_var}]"
    return None


def _get_elem_type_check(elem_type: str, var_name: str) -> str | None:
    if elem_type == "n":
        return f"not is_number({var_name})"
    if elem_type == "s":
        return f"not isinstance({var_name}, str)"
    if elem_type == "b":
        return f"not isinstance({var_name}, bool)"
    return None
