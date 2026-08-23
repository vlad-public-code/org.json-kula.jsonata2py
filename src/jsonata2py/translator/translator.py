"""Translates a JSONata AST into a complete, compilable Python source file.

Ported from org.json_kula.jsonata_jvm.translator.Translator.

The generated module:
  - imports everything from jsonata2py.runtime via `import *`;
  - defines a single _evaluate(_root, _ctx=None) entry point;
  - is stateless (pure functions) and therefore thread-safe.

Code-generation strategy: the visitor produces Python *expressions* for
every AST node. Block and VariableBinding nodes -- which require
statement-level code -- are compiled into self-contained top-level helper
`def`s (block_codegen.py) so call sites remain single expressions.

Usage:
    ast = optimize(Parser.parse("Account.Name"))
    src = Translator.translate(ast, "AccountName")
"""

from __future__ import annotations

import math
from typing import ClassVar

from ..errors import TranslatorError
from ..parser.ast_nodes import (
    ArrayConstructor,
    ArraySubscript,
    AstNode,
    BinaryOp,
    Block,
    BooleanLiteral,
    ChainExpr,
    CoalesceExpr,
    ConditionalExpr,
    ContextBinding,
    ContextRef,
    DescendantStep,
    ElvisExpr,
    FieldRef,
    ForceArray,
    FunctionCall,
    GroupByExpr,
    Lambda,
    LambdaCall,
    NullLiteral,
    NumberLiteral,
    ObjectConstructor,
    Parenthesized,
    ParentStep,
    PartialApplication,
    PartialPlaceholder,
    PathExpr,
    PositionBinding,
    PredicateExpr,
    RangeExpr,
    RegexLiteral,
    RootRef,
    SortExpr,
    SortKey,
    StringLiteral,
    TransformExpr,
    TransformLambda,
    UnaryMinus,
    VariableBinding,
    VariableRef,
    Visitor,
    WildcardStep,
    accept,
)
from . import call_codegen, path_codegen, scope_analyzer
from .block_codegen import visit_block as _visit_block
from .gen_ctx import GenCtx
from .gen_state import GenState
from .module_assembler import build_module
from .naming import py_string, pyvar, pyvar_ref

# JSONata built-in function names (without $ prefix) mapped to their
# runtime function names. When a VariableRef with one of these names is
# encountered outside a local scope (used as a first-class value, e.g. in
# a ~> chain), the translator wraps it in an inline lambda so it can be
# stored as a value and later invoked via fn_apply.
_BUILTIN_LAMBDA_WRAPPERS = {
    "uppercase": "fn_uppercase",
    "lowercase": "fn_lowercase",
    "trim": "fn_trim",
    "string": "fn_string",
    "number": "fn_number",
    "boolean": "fn_boolean",
    "not": "fn_not",
    "length": "fn_length",
    "exists": "fn_exists",
    "keys": "fn_keys",
    "values": "fn_values",
    "count": "fn_count",
    "sum": "fn_sum",
    "max": "fn_max",
    "min": "fn_min",
    "reverse": "fn_reverse",
    "flatten": "fn_flatten",
    "distinct": "fn_distinct",
    "shuffle": "fn_shuffle",
    "spread": "fn_spread",
    "merge": "fn_merge",
}

# Built-ins that take 2 arguments, used as first-class values.
_BUILTIN_BINARY_LAMBDA_WRAPPERS = {
    "append": "fn_append",
}

_ARITH_OPS = {"+", "-", "*", "/", "%"}


def _is_arith_op(op: str) -> bool:
    return op in _ARITH_OPS


# Operators whose emitted call already returns a Python bool, so `and`/`or`
# can consume the result directly. The ordering operators are deliberately
# absent: lt/le/gt/ge return MISSING when an operand is missing, so they
# still need is_truthy() to coerce.
_BOOL_RESULT_OPS = {"=", "!=", "in", "and", "or"}


def _as_bool(node: AstNode, expr: str) -> str:
    """Wraps expr in is_truthy() unless it is already known to be a bool.

    Used to emit `and`/`or` as Python's own short-circuiting operators.
    `and_(a, lambda: b)` / `or_(a, lambda: b)` are exactly
    `is_truthy(a) and/or is_truthy(b)`, but the helper form allocates a
    closure per operand per element purely to defer evaluation that
    Python's own operators defer for free.
    """
    if isinstance(node, BinaryOp) and node.op in _BOOL_RESULT_OPS:
        return expr
    return f"is_truthy({expr})"


class _TypedExpr:
    """A Python expression paired with a type flag. When numeric=True the
    expression evaluates to a float where NaN is the "missing" sentinel.
    When numeric=False it evaluates to a plain value."""

    __slots__ = ("code", "numeric")

    def __init__(self, code: str, numeric: bool) -> None:
        self.code = code
        self.numeric = numeric

    def as_double(self, op: str, is_left: bool) -> str:
        if self.numeric:
            return self.code
        fn = "num_val_l" if is_left else "num_val_r"
        return f'{fn}({self.code}, "{op}")'

    def as_value(self) -> str:
        return f"num_wrap({self.code})" if self.numeric else self.code


class Translator(Visitor[str, GenCtx]):
    @staticmethod
    def translate(ast: AstNode, source_expression: str = "") -> str:
        """Translates ast into a Python 3.11+ source file, embedding
        source_expression so CompiledExpression.source_jsonata returns the
        original JSONata text."""
        state = GenState()
        ctx = GenCtx("_ctx", "_root", state)
        t = Translator()

        body_expr = accept(ast, t, ctx)

        return build_module(
            body_expr,
            "\n".join(state.helper_defs),
            "\n".join(state.local_declarations),
            source_expression,
            state.constant_declarations(),
        )

    # =========================================================================
    # Literals and references
    # =========================================================================

    def visit_string_literal(self, n: StringLiteral, ctx: GenCtx) -> str:
        # Scalars are already interned in the code object -- no hoisting
        # (D3/6.4), unlike Java which hoists every literal to a static field.
        return py_string(n.value)

    def visit_number_literal(self, n: NumberLiteral, ctx: GenCtx) -> str:
        v = n.value
        # isfinite() must be tested FIRST: math.floor(inf) raises OverflowError
        # and math.floor(nan) raises ValueError, so a non-finite literal would
        # crash codegen before the guard could reject it.
        if math.isfinite(v) and v == math.floor(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)

    def visit_boolean_literal(self, n: BooleanLiteral, ctx: GenCtx) -> str:
        return "True" if n.value else "False"

    def visit_null_literal(self, n: NullLiteral, ctx: GenCtx) -> str:
        return "None"

    def visit_regex_literal(self, n: RegexLiteral, ctx: GenCtx) -> str:
        return f"regex_value({py_string(n.pattern)}, {py_string(n.flags)})"

    def visit_context_ref(self, n: ContextRef, ctx: GenCtx) -> str:
        return ctx.ctx_var

    def visit_root_ref(self, n: RootRef, ctx: GenCtx) -> str:
        return ctx.root_var

    def visit_variable_ref(self, n: VariableRef, ctx: GenCtx) -> str:
        if ctx.state.is_local(n.name):
            if n.name in ctx.state.holder_vars:
                return f"{pyvar_ref(n.name)}[0]"
            alias = ctx.state.get_alias(n.name)
            return alias if alias is not None else pyvar(n.name)
        wrapper = _BUILTIN_LAMBDA_WRAPPERS.get(n.name)
        if wrapper is not None:
            return f"lambda_value(lambda _b: {wrapper}(_b), 1)"
        binary_wrapper = _BUILTIN_BINARY_LAMBDA_WRAPPERS.get(n.name)
        if binary_wrapper is not None:
            return (
                f"lambda_value(lambda _b: {binary_wrapper}("
                f"_b[0] if isinstance(_b, list) else _b, "
                f"_b[1] if isinstance(_b, list) and len(_b) > 1 else MISSING), 2)"
            )
        return f"resolve_binding({py_string(n.name)})"

    def visit_field_ref(self, n: FieldRef, ctx: GenCtx) -> str:
        return f"field({ctx.ctx_var}, {py_string(n.name)})"

    def visit_wildcard_step(self, n: WildcardStep, ctx: GenCtx) -> str:
        return f"wildcard({ctx.ctx_var})"

    def visit_descendant_step(self, n: DescendantStep, ctx: GenCtx) -> str:
        return f"descendant({ctx.ctx_var})"

    def visit_parent_step(self, n: ParentStep, ctx: GenCtx) -> str:
        if not ctx.parent_vars:
            raise TranslatorError("S0217", "Parent operator % used with no parent context")
        return ctx.parent_vars[-1]

    def visit_position_binding(self, n: PositionBinding, ctx: GenCtx) -> str:
        raise TranslatorError(None, "PositionBinding must appear inside a PathExpr")

    def visit_context_binding(self, n: ContextBinding, ctx: GenCtx) -> str:
        raise TranslatorError(None, "ContextBinding must appear inside a PathExpr")

    # =========================================================================
    # Path expressions
    # =========================================================================

    def visit_path_expr(self, n: PathExpr, ctx: GenCtx) -> str:
        return path_codegen.visit_path_expr(self, n, ctx)

    def visit_force_array(self, n: ForceArray, ctx: GenCtx) -> str:
        if path_codegen.path_ends_with_array_constructor(n.source):
            return accept(n.source, self, ctx.with_array_constructor_preserve())
        return f"force_array({accept(n.source, self, ctx)})"

    def visit_predicate_expr(self, n: PredicateExpr, ctx: GenCtx) -> str:
        return path_codegen.visit_predicate_expr(self, n, ctx)

    def visit_array_subscript(self, n: ArraySubscript, ctx: GenCtx) -> str:
        src_expr = accept(n.source, self, ctx)
        idx_expr = accept(n.index, self, ctx)
        sub = f"subscript({src_expr}, {idx_expr})"
        return f"force_array({sub})" if _source_chain_contains_force_array(n.source) else sub

    def visit_parenthesized(self, n: Parenthesized, ctx: GenCtx) -> str:
        return accept(n.inner, self, ctx)

    # =========================================================================
    # Numeric specialisation
    # =========================================================================

    def _expr_typed(self, node: AstNode, ctx: GenCtx) -> _TypedExpr:
        if isinstance(node, BinaryOp) and _is_arith_op(node.op):
            left = self._expr_typed(node.left, ctx)
            right = self._expr_typed(node.right, ctx)
            lc = left.as_double(node.op, True)
            rc = right.as_double(node.op, False)
            if node.op == "+":
                code = f"({lc} + {rc})"
            elif node.op == "-":
                code = f"({lc} - {rc})"
            elif node.op == "*":
                code = f"mul_d({lc}, {rc})"
            elif node.op == "/":
                code = f"div_d({lc}, {rc})"
            else:
                code = f"mod_d({lc}, {rc})"
            return _TypedExpr(code, True)
        if isinstance(node, UnaryMinus):
            inner = self._expr_typed(node.operand, ctx)
            code = f"(-{inner.code})" if inner.numeric else f"neg_dn({inner.code})"
            return _TypedExpr(code, True)
        if isinstance(node, NumberLiteral):
            v = node.value
            if v != v or math.isinf(v):
                return _TypedExpr(accept(node, self, ctx), False)
            lit = str(int(v)) if v == math.floor(v) and abs(v) < 1e15 else repr(v)
            return _TypedExpr(lit, True)
        return _TypedExpr(accept(node, self, ctx), False)

    # =========================================================================
    # Operators
    # =========================================================================

    def visit_binary_op(self, n: BinaryOp, ctx: GenCtx) -> str:
        op_ctx = ctx.with_tail_position(False)
        if _is_arith_op(n.op):
            return self._expr_typed(n, op_ctx).as_value()
        left = accept(n.left, self, op_ctx)
        right = accept(n.right, self, op_ctx)
        if n.op == "&":
            return f"concat({left}, {right})"
        if n.op == "=":
            return f"eq({left}, {right})"
        if n.op == "!=":
            return f"ne({left}, {right})"
        if n.op == "<":
            return f"lt({left}, {right})"
        if n.op == "<=":
            return f"le({left}, {right})"
        if n.op == ">":
            return f"gt({left}, {right})"
        if n.op == ">=":
            return f"ge({left}, {right})"
        if n.op == "and":
            return f"({_as_bool(n.left, left)} and {_as_bool(n.right, right)})"
        if n.op == "or":
            return f"({_as_bool(n.left, left)} or {_as_bool(n.right, right)})"
        if n.op == "in":
            return f"in_({left}, {right})"
        raise TranslatorError(None, f"Unknown operator: {n.op}")

    def visit_unary_minus(self, n: UnaryMinus, ctx: GenCtx) -> str:
        return self._expr_typed(n, ctx.with_tail_position(False)).as_value()

    # =========================================================================
    # Conditional
    # =========================================================================

    def visit_conditional_expr(self, n: ConditionalExpr, ctx: GenCtx) -> str:
        cond = accept(n.condition, self, ctx.with_tail_position(False))
        then = accept(n.then, self, ctx)
        otherwise = accept(n.otherwise, self, ctx) if n.otherwise is not None else "MISSING"
        return f"({then} if is_truthy({cond}) else {otherwise})"

    def visit_elvis_expr(self, n: ElvisExpr, ctx: GenCtx) -> str:
        return f"elvis({accept(n.left, self, ctx)}, {accept(n.right, self, ctx)})"

    def visit_coalesce_expr(self, n: CoalesceExpr, ctx: GenCtx) -> str:
        return f"coalesce({accept(n.left, self, ctx)}, {accept(n.right, self, ctx)})"

    def visit_partial_placeholder(self, n: PartialPlaceholder, ctx: GenCtx) -> str:
        if ctx.state.partial_ph_var is None:
            raise TranslatorError("T1008", "The ?? operator should only appear in a function call")
        if ctx.state.partial_ph_need_idx:
            idx = ctx.state.partial_ph_idx
            ctx.state.partial_ph_idx += 1
            return f"{ctx.state.partial_ph_var}[{idx}]"
        return ctx.state.partial_ph_var

    def visit_partial_application(self, n: PartialApplication, ctx: GenCtx) -> str:
        ph_count = sum(1 for a in n.args if isinstance(a, PartialPlaceholder))
        id_ = ctx.state.next_id()

        if ph_count == 1:
            ph_var = f"_ph{id_}"
            need_idx = False
        else:
            ph_var = f"_pak{id_}"
            need_idx = True

        saved_ph_var = ctx.state.partial_ph_var
        saved_need_idx = ctx.state.partial_ph_need_idx
        saved_ph_idx = ctx.state.partial_ph_idx
        ctx.state.partial_ph_var = ph_var
        ctx.state.partial_ph_need_idx = need_idx
        ctx.state.partial_ph_idx = 0

        call_body = self.visit_function_call(FunctionCall(n.name, n.args), ctx)

        ctx.state.partial_ph_var = saved_ph_var
        ctx.state.partial_ph_need_idx = saved_need_idx
        ctx.state.partial_ph_idx = saved_ph_idx

        return f"lambda_value(lambda {ph_var}: {call_body}, 1)"

    # =========================================================================
    # Function calls and lambdas
    # =========================================================================

    _CALLBACK_BUILTINS: ClassVar[set[str]] = {"map", "filter", "single", "sift", "each", "sort", "reduce"}
    _UNTRANSLATED_CALLBACK = "__callback_translated_by_builtin__"

    def _is_literal(self, node: AstNode) -> bool:
        return isinstance(node, (StringLiteral, NumberLiteral, BooleanLiteral, NullLiteral))

    def _try_fused_call(self, n: FunctionCall, ctx: GenCtx, arg_ctx: GenCtx) -> str | None:
        arg0 = n.args[0]

        if n.name == "count":
            if (
                isinstance(arg0, PredicateExpr)
                and not isinstance(arg0.source, ForceArray)
                and not isinstance(arg0.predicate, RangeExpr)
                and not ctx.state.contains_parent_step(arg0.predicate)
            ):
                if (
                    isinstance(arg0.predicate, BinaryOp)
                    and arg0.predicate.op == "="
                    and isinstance(arg0.predicate.left, FieldRef)
                    and self._is_literal(arg0.predicate.right)
                ):
                    return (
                        f"fn_count_field_eq({accept(arg0.source, self, arg_ctx)}, "
                        f"{py_string(arg0.predicate.left.name)}, {accept(arg0.predicate.right, self, arg_ctx)})"
                    )
                src_expr = accept(arg0.source, self, arg_ctx)
                elem_var = f"_cf{ctx.state.next_id()}"
                pred_expr = accept(arg0.predicate, self, arg_ctx.with_ctx(elem_var))
                return f"fn_count_filter({src_expr}, lambda {elem_var}: {pred_expr})"
            if (
                isinstance(arg0, PathExpr)
                and len(arg0.steps) == 2
                and isinstance(arg0.steps[1], FieldRef)
                and not ctx.state.contains_parent_step(arg0)
            ):
                src_expr = accept(arg0.steps[0], self, arg_ctx)
                return f"fn_count_field({src_expr}, {py_string(arg0.steps[1].name)})"

        fused_fn = {"sum": "fn_sum_field", "average": "fn_average_field", "max": "fn_max_field", "min": "fn_min_field"}.get(
            n.name
        )
        if fused_fn is not None and isinstance(arg0, PathExpr) and not ctx.state.contains_parent_step(arg0):
            if len(arg0.steps) == 2 and isinstance(arg0.steps[1], FieldRef):
                src_expr = accept(arg0.steps[0], self, arg_ctx)
                return f"{fused_fn}({src_expr}, {py_string(arg0.steps[1].name)})"
            if (
                len(arg0.steps) == 3
                and isinstance(arg0.steps[1], FieldRef)
                and isinstance(arg0.steps[2], FieldRef)
            ):
                src_expr = accept(arg0.steps[0], self, arg_ctx)
                return f"{fused_fn}({src_expr}, {py_string(arg0.steps[1].name)}, {py_string(arg0.steps[2].name)})"

        return None

    def visit_function_call(self, n: FunctionCall, ctx: GenCtx) -> str:
        arg_ctx = ctx.with_tail_position(False)
        if not ctx.state.is_local(n.name) and len(n.args) == 1:
            fused = self._try_fused_call(n, ctx, arg_ctx)
            if fused is not None:
                return fused

        callback_translated_by_builtin = not ctx.state.is_local(n.name) and n.name in self._CALLBACK_BUILTINS
        args: list[str] = []
        for arg in n.args:
            if callback_translated_by_builtin and isinstance(arg, Lambda):
                args.append(self._UNTRANSLATED_CALLBACK)
            else:
                args.append(accept(arg, self, arg_ctx))

        if ctx.state.is_local(n.name):
            return call_codegen.gen_user_function_call(self, n, args, ctx)

        return self._dispatch_builtin(n, args, ctx)

    def _dispatch_builtin(self, n: FunctionCall, args: list[str], ctx: GenCtx) -> str:
        from .module_assembler import ctx_arg, one_arg

        name = n.name
        a = args
        if name == "string":
            return (
                f"fn_string({ctx_arg(a, ctx.ctx_var)})" if len(a) <= 1 else f"fn_string({a[0]}, {a[1]})"
            )
        if name == "number":
            return f'fn_arity_error("number", 1, {len(a)})' if len(a) > 1 else f"fn_number({ctx_arg(a, ctx.ctx_var)})"
        if name == "boolean":
            return f'fn_arity_error("boolean", 1, {len(a)})' if len(a) > 1 else f"fn_boolean({ctx_arg(a, ctx.ctx_var)})"
        if name == "not":
            return f"fn_not({ctx_arg(a, ctx.ctx_var)})"
        if name == "type":
            return f"fn_type({ctx_arg(a, ctx.ctx_var)})"
        if name == "exists":
            return f'fn_arity_error("exists", 1, {len(a)})' if len(a) != 1 else f"fn_exists({a[0]})"
        if name == "floor":
            return f"fn_floor({ctx_arg(a, ctx.ctx_var)})"
        if name == "ceil":
            return f"fn_ceil({ctx_arg(a, ctx.ctx_var)})"
        if name == "round":
            return f"fn_round({ctx_arg(a, ctx.ctx_var)})" if len(a) <= 1 else f"fn_round({a[0]}, {a[1]})"
        if name == "abs":
            return f"fn_abs({ctx_arg(a, ctx.ctx_var)})"
        if name == "sqrt":
            return f"fn_sqrt({ctx_arg(a, ctx.ctx_var)})"
        if name == "power":
            return f"fn_power({a[0]}, {a[1]})"
        if name == "random":
            return "fn_random()"
        if name == "formatBase":
            return f"fn_formatBase({a[0]}, MISSING)" if len(a) == 1 else f"fn_formatBase({a[0]}, {a[1]})"
        if name == "formatNumber":
            return f"fn_formatNumber({a[0]}, {a[1]})" if len(a) == 2 else f"fn_formatNumber({a[0]}, {a[1]}, {a[2]})"
        if name == "formatInteger":
            return f"fn_formatInteger({a[0]}, {a[1]})"
        if name == "parseInteger":
            return f"fn_parseInteger({a[0]}, {a[1]})"
        if name == "uppercase":
            return f'fn_arity_error("uppercase", 1, {len(a)})' if len(a) > 1 else f"fn_uppercase({ctx_arg(a, ctx.ctx_var)})"
        if name == "lowercase":
            return f'fn_arity_error("lowercase", 1, {len(a)})' if len(a) > 1 else f"fn_lowercase({ctx_arg(a, ctx.ctx_var)})"
        if name == "trim":
            return f"fn_trim({ctx_arg(a, ctx.ctx_var)})"
        if name == "length":
            if len(a) > 1:
                return f'fn_arity_error("length", 1, {len(a)})'
            return f"fn_length_ctx({ctx.ctx_var})" if not a else f"fn_length({a[0]})"
        if name == "substring":
            if len(a) > 3:
                return f'fn_arity_error("substring", 3, {len(a)})'
            return f"fn_substring({a[0]}, {a[1]})" if len(a) == 2 else f"fn_substring({a[0]}, {a[1]}, {a[2]})"
        if name == "substringBefore":
            return f"fn_substringBefore_ctx({ctx.ctx_var}, {a[0]})" if len(a) == 1 else f"fn_substringBefore({a[0]}, {a[1]})"
        if name == "substringAfter":
            return f"fn_substringAfter_ctx({ctx.ctx_var}, {a[0]})" if len(a) == 1 else f"fn_substringAfter({a[0]}, {a[1]})"
        if name == "contains":
            return f"fn_contains({ctx.ctx_var}, {a[0]})" if len(a) == 1 else f"fn_contains({a[0]}, {a[1]})"
        if name == "split":
            if len(a) == 1:
                return f"fn_split({a[0]}, MISSING)"
            return f"fn_split({a[0]}, {a[1]})" if len(a) == 2 else f"fn_split({a[0]}, {a[1]}, {a[2]})"
        if name == "match":
            if len(a) == 1:
                return f"fn_match({ctx.ctx_var}, {a[0]})"
            return f"fn_match({a[0]}, {a[1]})" if len(a) == 2 else f"fn_match({a[0]}, {a[1]}, {a[2]})"
        if name == "replace":
            if len(a) < 2:
                return f'fn_arity_error("replace", 3, {len(a)})'
            if len(a) == 2:
                return f"fn_replace({ctx.ctx_var}, {a[0]}, {a[1]})"
            if len(a) == 3:
                return f"fn_replace({a[0]}, {a[1]}, {a[2]})"
            return f"fn_replace({a[0]}, {a[1]}, {a[2]}, {a[3]})"
        if name == "join":
            if not a:
                return 'fn_arity_error("join", 1, 0)'
            return f"fn_join({a[0]}, MISSING)" if len(a) == 1 else f"fn_join({a[0]}, {a[1]})"
        if name == "pad":
            return f"fn_pad({a[0]}, {a[1]})" if len(a) == 2 else f"fn_pad({a[0]}, {a[1]}, {a[2]})"
        if name == "eval":
            return f"fn_eval({a[0]}, {ctx.ctx_var})" if len(a) == 1 else f"fn_eval({a[0]}, {a[1]})"
        if name == "base64encode":
            return f"fn_base64encode({ctx_arg(a, ctx.ctx_var)})"
        if name == "base64decode":
            return f"fn_base64decode({ctx_arg(a, ctx.ctx_var)})"
        if name == "encodeUrlComponent":
            return f"fn_encodeUrlComponent({one_arg(a)})"
        if name == "decodeUrlComponent":
            return f"fn_decodeUrlComponent({one_arg(a)})"
        if name == "encodeUrl":
            return f"fn_encodeUrl({one_arg(a)})"
        if name == "decodeUrl":
            return f"fn_decodeUrl({one_arg(a)})"
        if name == "count":
            return f'fn_arity_error("count", 1, {len(a)})' if len(a) > 1 else f"fn_count({one_arg(a)})"
        if name == "sum":
            if len(a) > 1:
                return f'fn_arity_error("sum", 1, {len(a)})'
            if not a:
                return 'fn_arity_error("sum", 1, 0)'
            return f"fn_sum({a[0]})"
        if name == "max":
            return f'fn_arity_error("max", 1, {len(a)})' if len(a) > 1 else f"fn_max({one_arg(a)})"
        if name == "min":
            return f'fn_arity_error("min", 1, {len(a)})' if len(a) > 1 else f"fn_min({one_arg(a)})"
        if name == "average":
            return f'fn_arity_error("average", 1, {len(a)})' if len(a) > 1 else f"fn_average({one_arg(a)})"
        if name == "append":
            return f"fn_append({a[0]}, {a[1]})"
        if name == "reverse":
            return f"fn_reverse({one_arg(a)})"
        if name == "distinct":
            return f"fn_distinct({one_arg(a)})"
        if name == "flatten":
            return f"fn_flatten({one_arg(a)})"
        if name == "shuffle":
            return f"fn_shuffle({one_arg(a)})"
        if name == "zip":
            return f"fn_zip({', '.join(a)})"
        if name == "sort":
            return call_codegen.gen_sort(self, n, a, ctx)
        if name == "map":
            return f'fn_arity_error("map", 2, {len(n.args)})' if len(n.args) < 2 else call_codegen.gen_higher_order(
                self, "fn_map", n, a, ctx, 0, 1
            )
        if name == "filter":
            return call_codegen.gen_higher_order(self, "fn_filter", n, a, ctx, 0, 1)
        if name == "each":
            return call_codegen.gen_each(self, n, a, ctx)
        if name == "reduce":
            return call_codegen.gen_reduce(self, n, a, ctx)
        if name == "single":
            return call_codegen.gen_single(self, n, a, ctx)
        if name == "sift":
            return call_codegen.gen_sift(self, n, a, ctx)
        if name == "keys":
            return f"fn_keys({one_arg(a)})"
        if name == "values":
            return f"fn_values({one_arg(a)})"
        if name == "lookup":
            return f"fn_lookup({a[0]}, {a[1]})"
        if name == "spread":
            return f"fn_spread({one_arg(a)})"
        if name == "merge":
            return f"fn_merge({one_arg(a)})"
        if name == "assert":
            return f"fn_assert({a[0]}, {a[1] if len(a) > 1 else 'MISSING'})"
        if name == "now":
            if len(a) == 0:
                return "fn_now()"
            if len(a) == 1:
                return f"fn_now({a[0]})"
            return f"fn_now({a[0]}, {a[1]})"
        if name == "millis":
            return "fn_millis()"
        if name == "fromMillis":
            if len(a) == 0:
                return f"fn_fromMillis({ctx_arg(a, ctx.ctx_var)})"
            if len(a) == 1:
                return f"fn_fromMillis({a[0]})"
            if len(a) == 2:
                return f"fn_fromMillis({a[0]}, {a[1]})"
            return f"fn_fromMillis({a[0]}, {a[1]}, {a[2]})"
        if name == "toMillis":
            return f"fn_toMillis({a[0]})" if len(a) == 1 else f"fn_toMillis({a[0]}, {a[1]})"
        if name == "error":
            return f"fn_error({a[0] if a else 'MISSING'})"
        return call_codegen.gen_user_function_call(self, n, a, ctx)

    def visit_lambda(self, n: Lambda, ctx: GenCtx) -> str:
        # Standalone lambda: generate as an inline Python lambda so enclosing
        # block-locals are captured as closures. Declared parameter count
        # travels with the value so a built-in receiving it as a value knows
        # how many arguments to pass.
        return f"lambda_value({call_codegen.build_inline_lambda_with_sig(self, n, ctx)}, {len(n.params)})"

    def visit_lambda_call(self, n: LambdaCall, ctx: GenCtx) -> str:
        return call_codegen.gen_lambda_call(self, n, ctx)

    # =========================================================================
    # Variable binding and blocks
    # =========================================================================

    def visit_variable_binding(self, n: VariableBinding, ctx: GenCtx) -> str:
        return accept(n.value, self, ctx)

    def visit_block(self, n: Block, ctx: GenCtx) -> str:
        return _visit_block(self, n, ctx)

    # =========================================================================
    # Constructors
    # =========================================================================

    def visit_array_constructor(self, n: ArrayConstructor, ctx: GenCtx) -> str:
        if not n.elements:
            return "array_of()"
        if len(n.elements) == 1:
            elem = n.elements[0]
            if ctx.in_array_constructor_step and not isinstance(elem, ArrayConstructor):
                return f"force_array({accept(elem, self, ctx)})"
            elem_code = accept(elem, self, ctx)
            if isinstance(elem, ArrayConstructor):
                return f"array_of(preserve_array({elem_code}))"
            return f"array_of({elem_code})"
        elems = [self._wrap_array_element(e, ctx) for e in n.elements]
        return f"array_of({', '.join(elems)})"

    def _wrap_array_element(self, e: AstNode, ctx: GenCtx) -> str:
        if isinstance(e, RangeExpr):
            if isinstance(e.from_, NumberLiteral) and isinstance(e.to, NumberLiteral):
                return f"range_flatten({int(e.from_.value)}, {int(e.to.value)})"
            return accept(e, self, ctx)
        if isinstance(e, ArrayConstructor):
            return f"preserve_array({accept(e, self, ctx)})"
        return accept(e, self, ctx)

    def visit_object_constructor(self, n: ObjectConstructor, ctx: GenCtx) -> str:
        if not n.pairs:
            return "object_()"
        all_keys_literal = all(isinstance(p.key, StringLiteral) for p in n.pairs)
        if all_keys_literal:
            keys = [py_string(p.key.value) for p in n.pairs if isinstance(p.key, StringLiteral)]
            values = [accept(p.value, self, ctx) for p in n.pairs]
            key_field = ctx.state.key_array(f"[{', '.join(keys)}]")
            return f"object_of({key_field}, [{', '.join(values)}])"
        parts = []
        for p in n.pairs:
            parts.append(accept(p.key, self, ctx))
            parts.append(accept(p.value, self, ctx))
        return f"object_({', '.join(parts)})"

    # =========================================================================
    # Range, sort, group-by, chain, transform
    # =========================================================================

    def visit_range_expr(self, n: RangeExpr, ctx: GenCtx) -> str:
        return f"range_({accept(n.from_, self, ctx)}, {accept(n.to, self, ctx)})"

    def visit_sort_expr(self, n: SortExpr, ctx: GenCtx) -> str:
        has_parent_ref = any(ctx.state.contains_parent_step(k.key) for k in n.keys)
        if has_parent_ref and isinstance(n.source, PathExpr):
            return self._compile_sort_with_parent(n, n.source, ctx)
        src_expr = accept(n.source, self, ctx)
        result = src_expr
        for sk in reversed(n.keys):
            key_var = f"_sk{ctx.state.next_id()}"
            key_expr = accept(sk.key, self, ctx.with_ctx(key_var))
            sorted_call = f"fn_sort({result}, lambda {key_var}: {key_expr})"
            result = f"fn_reverse({sorted_call})" if sk.descending else sorted_call
        return f"unwrap({result})"

    @staticmethod
    def _max_parent_depth(keys: list[SortKey]) -> int:
        max_d = 0
        for sk in keys:
            if isinstance(sk.key, PathExpr):
                d = 0
                for step in sk.key.steps:
                    if isinstance(step, ParentStep):
                        d += 1
                    else:
                        break
                max_d = max(max_d, d)
        return max_d

    def _compile_sort_with_parent(self, n: SortExpr, source_path: PathExpr, ctx: GenCtx) -> str:
        depth = self._max_parent_depth(n.keys)
        steps = source_path.steps
        if depth < 1 or len(steps) < depth + 1:
            src_expr = accept(source_path, self, ctx)
            result = src_expr
            for sk in reversed(n.keys):
                key_var = f"_sk{ctx.state.next_id()}"
                key_expr = accept(sk.key, self, ctx.with_ctx(key_var))
                sorted_call = f"fn_sort({result}, lambda {key_var}: {key_expr})"
                result = f"fn_reverse({sorted_call})" if sk.descending else sorted_call
            return f"unwrap({result})"

        if depth == 1:
            if len(steps) == 1:
                prefix_expr = ctx.ctx_var
            else:
                prefix = PathExpr(list(steps[:-1]))
                prefix_expr = accept(prefix, self, ctx)
            elem_step = steps[-1]
            par_var = f"_par{ctx.state.next_id()}"
            elem_expr = path_codegen.apply_step(self, par_var, elem_step, ctx.with_ctx(par_var))
            tuple_expr = f"fn_collect_pairs({prefix_expr}, lambda {par_var}: {elem_expr})"
            parent_ref_exprs = ["_TUPLE[1]"]
        else:  # depth == 2
            if len(steps) == 2:
                gp_expr = ctx.ctx_var
            else:
                gp = PathExpr(list(steps[:-2]))
                gp_expr = accept(gp, self, ctx)
            parent_step = steps[-2]
            elem_step = steps[-1]
            gp_var = f"_gp{ctx.state.next_id()}"
            par_var = f"_par{ctx.state.next_id()}"
            parent_expr = path_codegen.apply_step(self, gp_var, parent_step, ctx.with_ctx(gp_var))
            elem_expr = path_codegen.apply_step(self, par_var, elem_step, ctx.with_ctx(par_var))
            tuple_expr = f"fn_collect_triples({gp_expr}, lambda {gp_var}: {parent_expr}, lambda {par_var}: {elem_expr})"
            parent_ref_exprs = ["_TUPLE[2]", "_TUPLE[1]"]

        result = tuple_expr
        for sk in reversed(n.keys):
            tuple_var = f"_tk{ctx.state.next_id()}"
            p_vars = [ref.replace("_TUPLE", tuple_var) for ref in parent_ref_exprs]
            key_expr = accept(sk.key, self, ctx.with_ctx(f"{tuple_var}[0]").with_parents(p_vars))
            sorted_call = f"fn_sort({result}, lambda {tuple_var}: {key_expr})"
            result = f"fn_reverse({sorted_call})" if sk.descending else sorted_call
        ext_var = f"_tex{ctx.state.next_id()}"
        return f"unwrap(fn_map({result}, lambda {ext_var}: {ext_var}[0]))"

    def visit_group_by_expr(self, n: GroupByExpr, ctx: GenCtx) -> str:
        src_expr = accept(n.source, self, ctx)
        if not n.pairs:
            return src_expr

        outer_locals: set[str] = set()
        for scope in ctx.state.scope_stack:
            outer_locals |= scope
        used_locals: set[str] = set()
        for p in n.pairs:
            scope_analyzer.collect_free_vars_into(p.key, used_locals, set())
            scope_analyzer.collect_free_vars_into(p.value, used_locals, set())
        used_locals &= outer_locals
        captured_vars = list(used_locals)
        if ctx.primary_context_var is not None:
            # primary_context_var is a compiled expression string (e.g. an
            # alias or plain local); best-effort strip to a bare name for
            # exclusion purposes, matching Java's stripping of the "$" sigil.
            captured_vars = [v for v in captured_vars if pyvar(v) != ctx.primary_context_var]

        extra_param_decls: list[str] = []
        extra_call_args: list[str] = []
        # Names whose *current* alias is a computed expression (e.g. a
        # #$pos pair-unpack: "(_pair0[1] if isinstance(_pair0, list) else
        # 0)") rather than a bound identifier can't be forwarded as-is --
        # the expression is only valid at THIS call site (it references a
        # variable, e.g. _pair0, local to the enclosing lambda), and the
        # groupBy helper below is a separate top-level def with no closure
        # over it. Fix: evaluate the expression once here (call arg), and
        # rebind the name to a fresh plain parameter (rebind_overrides)
        # before compiling the pair key/value expressions below, so their
        # compiled text references the new parameter instead.
        rebind_overrides: list[tuple[str, str]] = []
        for v in captured_vars:
            alias = ctx.state.get_alias(v)
            if alias is not None and not alias.isidentifier():
                fresh_name = f"_cap{ctx.state.next_id()}"
                extra_param_decls.append(fresh_name)
                extra_call_args.append(alias)
                rebind_overrides.append((v, fresh_name))
                continue
            py_name = alias if alias is not None else pyvar(v)
            extra_param_decls.append(py_name)
            if v in ctx.state.holder_vars:
                extra_call_args.append(f"{pyvar_ref(v)}[0]")
            else:
                extra_call_args.append(py_name)

        elem_var = f"_ge{ctx.state.next_id()}"
        elem_ctx = ctx.with_ctx(elem_var)
        if rebind_overrides:
            ctx.state.push_scope()
            for name, fresh_name in rebind_overrides:
                ctx.state.add_local_var_with_alias(name, fresh_name)
        try:
            pair_exprs: list[tuple[str, str]] = []
            for p in n.pairs:
                pair_exprs.append((accept(p.key, self, elem_ctx), accept(p.value, self, elem_ctx)))
        finally:
            if rebind_overrides:
                ctx.state.pop_scope()

        id_ = ctx.state.next_id()
        method_name = f"_groupBy{id_}"

        body_lines: list[str] = []
        body_lines.append("_result = {}")
        body_lines.append("_items = _src if isinstance(_src, list) else ([] if _src is MISSING else [_src])")
        for pi, (k_expr, v_expr) in enumerate(pair_exprs):
            grp_var = f"_grp{pi}"
            body_lines.append(f"{grp_var} = {{}}")
            body_lines.append(f"for {elem_var} in _items:")
            body_lines.append(f"    _kNode{pi} = {k_expr}")
            body_lines.append(
                f'    if _kNode{pi} is not MISSING and not isinstance(_kNode{pi}, str): '
                f'fn_throw("T1003", "The key of an object constructor must evaluate to a string")'
            )
            body_lines.append(f"    if _kNode{pi} is MISSING:")
            body_lines.append("        continue")
            body_lines.append(f"    {grp_var}.setdefault(_kNode{pi}, []).append({elem_var})")
            body_lines.append(f"for _k{pi}, _grpArr{pi} in {grp_var}.items():")
            body_lines.append(f"    {elem_var} = _grpArr{pi}[0] if len(_grpArr{pi}) == 1 else _grpArr{pi}")
            if ctx.primary_context_var is not None:
                body_lines.append(f"    {ctx.primary_context_var} = {elem_var}")
            body_lines.append(f"    _v{pi} = {v_expr}")
            body_lines.append(f"    if _v{pi} is not MISSING:")
            body_lines.append(f"        if _k{pi} in _result:")
            body_lines.append(
                f'            fn_throw("D1009", "Multiple key definitions evaluate to same key: \'" '
                f'+ str(_k{pi}) + "\'")'
            )
            body_lines.append(f"        _result[_k{pi}] = _v{pi}")
        body_lines.append("if _src is MISSING:")
        body_lines.append("    return MISSING")
        body_lines.append("return _result")

        def_params = ["_src", ctx.root_var, *extra_param_decls]
        def_lines = [f"def {method_name}({', '.join(def_params)}):"]
        def_lines.extend(f"    {line}" for line in body_lines)
        ctx.state.helper_defs.append("\n".join(def_lines) + "\n")

        call_args = ", ".join([src_expr, ctx.root_var, *extra_call_args])
        return f"{method_name}({call_args})"

    def visit_chain_expr(self, n: ChainExpr, ctx: GenCtx) -> str:
        expr = accept(n.steps[0], self, ctx)
        for step in n.steps[1:]:
            fn_expr = self._chain_step_to_lambda(step, ctx)
            expr = f"fn_pipe({expr}, {fn_expr})"
        return expr

    def _chain_step_to_lambda(self, step: AstNode, ctx: GenCtx) -> str:
        if isinstance(step, FunctionCall):
            args_with_pipe: list[AstNode] = [PartialPlaceholder(), *step.args]
            return self.visit_partial_application(PartialApplication(step.name, args_with_pipe), ctx)
        if isinstance(step, ForceArray) and isinstance(step.source, FunctionCall):
            fc = step.source
            args_with_pipe = [PartialPlaceholder(), *fc.args]
            inner_partial = self.visit_partial_application(PartialApplication(fc.name, args_with_pipe), ctx)
            id_ = ctx.state.next_id()
            arg_var = f"_cfaArg{id_}"
            return f"lambda_value(lambda {arg_var}: force_array(fn_apply({inner_partial}, {arg_var})), 1)"
        return accept(step, self, ctx)

    def visit_transform_expr(self, n: TransformExpr, ctx: GenCtx) -> str:
        src_expr = accept(n.source, self, ctx)
        loc_var = f"_tl{ctx.state.next_id()}"
        loc_expr = accept(n.pattern, self, ctx.with_ctx(loc_var))
        upd_var = f"_tu{ctx.state.next_id()}"
        upd_expr = accept(n.update, self, ctx.with_ctx(upd_var))
        del_expr = accept(n.delete, self, ctx) if n.delete is not None else "MISSING"
        return (
            f"fn_transform({src_expr}, lambda {loc_var}: {loc_expr}, "
            f"lambda {upd_var}: {upd_expr}, {del_expr})"
        )

    def visit_transform_lambda(self, n: TransformLambda, ctx: GenCtx) -> str:
        src_var = f"_ts{ctx.state.next_id()}"
        loc_var = f"_tl{ctx.state.next_id()}"
        loc_expr = accept(n.pattern, self, ctx.with_ctx(loc_var))
        upd_var = f"_tu{ctx.state.next_id()}"
        upd_expr = accept(n.update, self, ctx.with_ctx(upd_var))
        del_expr = accept(n.delete, self, ctx) if n.delete is not None else "MISSING"
        return (
            f"lambda_value(lambda {src_var}: fn_transform({src_var}, lambda {loc_var}: {loc_expr}, "
            f"lambda {upd_var}: {upd_expr}, {del_expr}), 1)"
        )


def _source_chain_contains_force_array(node: AstNode) -> bool:
    if isinstance(node, ForceArray):
        return True
    if isinstance(node, SortExpr):
        return _source_chain_contains_force_array(node.source)
    if isinstance(node, PredicateExpr):
        return _source_chain_contains_force_array(node.source)
    if isinstance(node, PathExpr):
        return any(_source_chain_contains_force_array(s) for s in node.steps)
    return False
